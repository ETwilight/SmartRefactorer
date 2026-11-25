"""Run method-adapter policies through task episodes with persistent state and explicit
continuation.
"""

import argparse
import copy
import hashlib
from html import escape
import json
from pathlib import Path
import sys
import time
import shutil
from urllib.parse import urlsplit
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime.layout import RESULTS_ROOT
from runtime.models.transport import observe_model_calls
from runtime.models.transport import INPUT_ENCODING
from runtime.models.transport import CACHE_POLICY
from runtime.models.transport import ModelOutputError
from runtime.contracts import obj
from runtime.contracts import validate
from audit import RunRecorder, TokenBudgetExceeded
from audit.recorder import save_json, utc_now, summarize_usage
from audit.projection import ProgressAuditor
from audit.control import RunControl, RunStopped
from runtime.lifecycle import managed_environment
from runtime.feedback_semantics import is_read_only, operation_succeeded

DECISION = obj(
    {
        'decision': {'type': 'string', 'enum': ['request', 'finish']},
        'request_json': {'type': 'string'},
        'report': {'type': 'string'},
    }
)


class _TaskWorkspaceReleaseError(RuntimeError):
    """End task processing when releasing the current workspace fails.

    The message retains the release error; close_round() records its details in
    the round before raising this internal exception. run() catches it and enters
    finalization with the recorded failure status.

    Example, inside the workspace-release exception handler:
        raise _TaskWorkspaceReleaseError(str(exc)) from exc

    This leaves the task stream at the current task while the run is finalized.
    """

    pass


def task_index(feedback):
    """Wrap a successful task catalogue as an index with a tasks.get details operation."""

    if not operation_succeeded(feedback):
        return feedback
    return {
        **feedback,
        'result': {
            'view': 'task_index',
            'details_op': 'tasks.get',
            'tasks': copy.deepcopy(feedback['result']),
        },
    }


class SessionRunner:
    """Run a method adapter through task episodes and environment feedback.

    environment is a ManagedEnvironment closed during finalization.
    policy supplies decide(context) and may provide persistence,
    request-stream, feedback, round-end, and summary hooks. Policies
    with programmatic_continuation can advance their local execution
    state between model calls. Requests and model calls share one
    RunRecorder with cumulative token limits and optional episode caps.

    output_root is the Path containing run directories. compact_discovery
    defaults to true. auditor_factory supplies a round auditor.
    resume_from selects a stopped parent run; continuation_service loads
    its state, restores the recorder, copies context files, and saves
    new runner checkpoints. policy.persist() saves method-owned state
    whenever the runner checkpoints.

    Example:
        runner = SessionRunner(
            environment,
            policy,
            token_budget=token_budget,
            workflow=workflow,
            continuation_service=continuation_service,
        )
        report = runner.run(goal)

    The adapter's policy decides the next requests, receives their full
    recorded feedback, and retains its algorithm state while the runner
    handles task episodes, usage, checkpointing, and final cleanup.
    """

    def __init__(
        self,
        environment,
        policy,
        max_rounds=None,
        rounds=None,
        token_budget=50000000,
        round_token_budget=None,
        auditor_factory=None,
        workflow='task_session',
        output_root=None,
        compact_discovery=None,
        resume_from=None,
        continuation_service=None,
    ):
        """Bind the managed environment, policy, limits, reporting, and continuation
        settings.
        """

        if max_rounds is not None and (type(max_rounds) is not int or max_rounds < 1):
            raise ValueError('max_rounds must be positive or None')
        if round_token_budget is not None and round_token_budget <= 0:
            raise ValueError('Explicit trial round token budget must be positive')
        if rounds is not None and (
            type(rounds) is not int
            or rounds < 1
            or (max_rounds is not None and rounds > max_rounds)
        ):
            raise ValueError('rounds exceeds caller bound')
        self.environment = managed_environment(environment)
        self.policy = policy
        self.max_rounds = rounds or max_rounds
        self.token_budget = token_budget
        self.round_token_budget = round_token_budget
        self.workflow = workflow
        self.output_root = Path(output_root) if output_root else RESULTS_ROOT
        self.compact_discovery = True if compact_discovery is None else compact_discovery
        self.auditor_factory = auditor_factory or (
            lambda folder: ProgressAuditor(folder, schematics=False)
        )
        self.resume_from = Path(resume_from).resolve() if resume_from else None
        self.continuation_service = continuation_service

    def run(self, goal):
        """Discover the environment, run task decisions and requests, and return the
        finalized report.

        Restore an explicitly selected continuation before entering the task
        loops. Persist feedback and checkpoints as work progresses, then finish
        the active round, close the environment, and write the report.
        """
        from runtime.layout import run_identity

        # Create the segment directory shared by its report, context files, and ledger.
        run_id, folder = run_identity(self.output_root)
        recorder = RunRecorder(
            folder, run_id, self.workflow, self.token_budget, self.round_token_budget
        )
        auditor = None
        result = {
            'schema_version': '2.0',
            'run_id': run_id,
            'workflow': self.workflow,
            'goal': goal,
            'status': 'running',
            'started_at': recorder.started_at,
            'limits': {
                'max_rounds': self.max_rounds,
                'total_tokens': self.token_budget,
                'round_tokens': self.round_token_budget,
            },
            'budget_policy': (
                'total_only'
                if self.max_rounds is None and self.round_token_budget is None
                else 'explicit_trial_limits'
            ),
            'discovery_profile': 'manifest-index' if self.compact_discovery else 'legacy',
            'round_definition': 'task_episode_v1',
            'discovery': {},
            'rounds': recorder.rounds,
            'evaluator_results': [],
            'model_claim': None,
        }
        config = {
            'schema_version': '2.0',
            'run_id': run_id,
            'workflow': self.workflow,
            'goal': goal,
            'limits': result['limits'],
            'instructions': getattr(self.policy, 'instructions', ''),
            'discovery_profile': result['discovery_profile'],
            'budget_policy': result['budget_policy'],
            'round_definition': result['round_definition'],
            'decision_schema': getattr(self.policy, 'decision_schema', DECISION),
            'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'model_input_encoding': INPUT_ENCODING,
            'model_cache_policy': CACHE_POLICY,
            'model_cache_ttl': '30m',
            'model_transport_sha256': hashlib.sha256(
                (
                    Path(__file__).resolve().parents[1] / 'runtime/models/transport.py'
                ).read_bytes()
            ).hexdigest(),
            'model_input_codec_sha256': hashlib.sha256(
                (Path(__file__).resolve().parents[1] / 'runtime/context/codec.py').read_bytes()
            ).hexdigest(),
            'audit_configuration': {},
        }
        config['feedback_transport'] = (
            'scoped-feedback-v2'
            if getattr(self.policy, 'compact_transport', False)
            else 'full-history-v1'
        )
        save_json(folder / 'config.json', config)
        control = RunControl(folder / 'control.json', self.max_rounds)
        recorder.model_guard = lambda: control.check(recorder)
        # context: dict passed by copy to the policy; history contains decision and feedback
        # records.
        context = {'goal': goal, 'limits': result['limits'], 'discovery': {}, 'history': []}
        # resume: dict | None; a loaded continuation carries reports, memory, calls, and pending
        # requests.
        resume = None
        # resume_pending: list[dict] of parent-segment requests not yet dispatched.
        resume_pending = []
        carried_models = 0
        carried_tokens = 0
        # Restore carried usage and received feedback before dispatching the unexecuted request
        # tail.
        if self.resume_from is not None:
            resume = self.continuation_service.load_continuation(
                self.resume_from, readonly=getattr(self.environment, 'readonly', None)
            )
            if (
                goal != resume['report']['goal']
                or result['limits'] != resume['report']['limits']
            ):
                raise ValueError(
                    'Continuation must retain the original goal and cumulative budget/round caps'
                )
            self.continuation_service.restore_recorder(recorder, resume)
            result['rounds'] = recorder.rounds
            result['evaluator_results'] = copy.deepcopy(resume['report']['evaluator_results'])
            context['history'] = copy.deepcopy(resume['history'])
            context['history_offset'] = resume['history_total'] - len(resume['history'])
            resume_pending = copy.deepcopy(resume['pending_requests'])
            carried_models = len(recorder.models)
            carried_tokens = recorder.charged
            result['continuation'] = {
                k: resume[k] for k in ('source_run_id', 'source_report_sha256')
            }
            result['continuation'].update(
                carried_tokens=carried_tokens,
                carried_model_calls=carried_models,
                completed_rounds=len(recorder.rounds),
                resumed_round=recorder.round['round'],
            )
            result['continuation']['copied_context_files'] = (
                self.continuation_service.copy_context_sources(self.resume_from, folder)
            )
        if getattr(self.environment, 'continuation', None):
            continuation = copy.deepcopy(self.environment.continuation)
            context['continuation'] = continuation.get('handoff', {})
            result.setdefault('continuation', {}).update(
                {
                    k: continuation[k]
                    for k in ('source_run_id', 'carried_tokens', 'passed_tasks')
                    if k in continuation
                }
            )
            config['continuation'] = copy.deepcopy(result['continuation'])
        # evaluation_ids: set[str], deduplicating accepted evaluation reports across decisions
        # and segments.
        evaluation_ids = set()
        boundary_ids = set()
        audit_seconds = 0
        # active_stream: Iterator[dict] | None, retained for cleanup if request dispatch is
        # interrupted.
        active_stream = None
        stop = False
        if resume is not None:
            evaluation_ids.update((r['evaluation_id'] for r in result['evaluator_results']))
            boundary_ids.update(
                (
                    c['request']['request_id']
                    for c in recorder.env_calls
                    if c['request']['op'] == 'evaluation.run'
                )
            )
        catalogue_ids = set()
        task_stream = getattr(self.environment, 'task_stream', None)

        # These helpers share the current run state for checkpointing, discovery, and episode
        # completion.
        def checkpoint():
            """Persist the policy and save an active runner checkpoint through the
            continuation service.
            """

            if hasattr(self.policy, 'persist'):
                self.policy.persist()
            if self.continuation_service is not None and recorder.round is not None:
                self.continuation_service.save_runner_checkpoint(
                    folder, context, recorder, getattr(self.policy, 'memory', '')
                )

        def has_primary_failure():
            """Check whether the result already records an error, stop notice, or budget
            failure.
            """

            return bool(
                result.get('error')
                or result.get('control_notice')
                or result['status']
                in ('interrupted', 'token_budget_exhausted', 'accounting_blocked')
            )

        def budget_reason():
            """Map the recorder's exhausted limit to the corresponding run or round status."""

            return (
                'accounting_blocked'
                if recorder.budget_limit == 'accounting'
                else (
                    'round_token_budget_exhausted'
                    if recorder.budget_limit == 'round'
                    else 'token_budget_exhausted'
                )
            )

        def discover(ops):
            """Execute discovery requests and update the recorded and policy-facing
            environment views.
            """

            for op in ops:
                params = {}
                if getattr(self.policy, 'compact_transport', False):
                    if op == 'environment.describe':
                        params = {'detail': 'summary'}
                    if op == 'actions.list':
                        params = {'detail': 'index'}
                method = (
                    'compact_discovery_params' if self.compact_discovery else 'discovery_params'
                )
                if hasattr(self.environment, method):
                    params = getattr(self.environment, method)(op)
                elif hasattr(self.environment, 'discovery_params'):
                    params = self.environment.discovery_params(op)
                request = {
                    'protocol_version': '1.0',
                    'request_id': str(uuid.uuid4()),
                    'op': op,
                    'params': params,
                }
                feedback, call = recorder.observe_env(
                    request, self.environment.handle, stage='discovery'
                )
                result['discovery'][op] = call
                save_json(folder / 'discovery.json', result['discovery'])
                if not operation_succeeded(feedback):
                    raise RuntimeError('Discovery failed: ' + op)
                if op == 'tasks.list':
                    tasks = feedback['result']
                    if isinstance(tasks, dict):
                        # tasks changes from a task-index dict to its list of task definitions.
                        tasks = tasks.get('tasks', [])
                    catalogue_ids.clear()
                    catalogue_ids.update(
                        (
                            t['id']
                            for t in tasks
                            if isinstance(t, dict) and isinstance(t.get('id'), str)
                        )
                    )
                if op == 'environment.describe' and 'task_selection' in feedback['result']:
                    selection = copy.deepcopy(feedback['result']['task_selection'])
                    config['environment_task_selection'] = selection
                    result['environment_task_selection'] = selection
                project = getattr(self.environment, 'task_index', task_index)
                context['discovery'][op] = project(feedback) if op == 'tasks.list' else feedback
            save_json(folder / 'config.json', config)

        def advance_task(outcome, claim=None):
            """Advance the task stream with its outcome and persist the updated public
            progress.
            """

            current = task_stream.current_id
            task_stream.advance(outcome)
            recorder.event(
                'task_stream.advance',
                task_id=current,
                outcome=outcome,
                model_claim=claim,
                round_number=len(recorder.rounds),
                charged_tokens=recorder.charged,
            )
            recorder.active_task = None
            result['task_stream'] = task_stream.public_state()
            result['environment_task_selection'] = {
                'mode': 'task_stream',
                **copy.deepcopy(result['task_stream']),
            }
            config['environment_task_selection'] = copy.deepcopy(
                result['environment_task_selection']
            )
            save_json(folder / 'config.json', config)
            save_json(folder / 'task_stream_progress.json', result['task_stream'])
            return task_stream.current_id is None

        def close_round(reason):
            """Finish the round, call the policy hook, capture its audit, and release the
            task workspace.
            """

            nonlocal audit_seconds, stop
            record = recorder.finish_round(reason)
            if 'continuation_timing' in record:
                timing = record['continuation_timing']
                for name in ('wall_seconds', 'model_wall_seconds', 'env_wall_seconds'):
                    timing['segment_' + name] = max(0, record[name] - timing['carried_' + name])
            if hasattr(self.policy, 'on_round_end'):
                try:
                    if not getattr(self.policy, 'records_only_on_round_end', False):
                        control.check(recorder)
                    self.policy.on_round_end(copy.deepcopy(record))
                except RunStopped as exc:
                    result['status'] = exc.reason
                    result['control_notice'] = str(exc)
                    stop = True
                    recorder.event(
                        'operator.stop',
                        reason=exc.reason,
                        message=str(exc),
                        control=control.snapshot(),
                    )
                except Exception as exc:
                    result['status'] = 'failed'
                    stop = True
                    result['error'] = {
                        'type': type(exc).__name__,
                        'message': str(exc),
                        'stage': 'workflow.on_round_end',
                    }
                    recorder.event('workflow.hook_error', error=result['error'])
            started = time.perf_counter()
            try:
                record['audit'] = auditor.capture(
                    record,
                    [c for c in recorder.env_calls if c['stage'] == 'workflow'],
                    recorder.instances,
                )
            except Exception as exc:
                record['audit'] = {
                    'status': 'error',
                    'error': {'type': type(exc).__name__, 'message': str(exc)},
                    'wall_seconds': time.perf_counter() - started,
                    'model_calls': 0,
                    'task_tokens': 0,
                }
            audit_seconds += time.perf_counter() - started
            recorder.event('audit.finish', round_number=record['round'], audit=record['audit'])
            if task_stream is not None and hasattr(self.environment, 'release_task_workspace'):
                try:
                    receipt = self.environment.release_task_workspace(reason)
                    if receipt is not None:
                        record['task_workspace_release'] = receipt
                except Exception as exc:
                    stop = True
                    error = {
                        'type': type(exc).__name__,
                        'message': str(exc),
                        'stage': 'task_workspace.release',
                    }
                    record['task_workspace_release_error'] = copy.deepcopy(error)
                    if has_primary_failure():
                        result.setdefault('finalization_errors', []).append(error)
                    else:
                        result['status'] = 'task_workspace_release_failed'
                        result['error'] = error
                    raise _TaskWorkspaceReleaseError(str(exc)) from exc
            save_json(folder / f"rounds/{record['round']:04d}.json", record)
            save_json(folder / 'progress.json', result)

        try:
            if hasattr(self.policy, 'bind'):
                self.policy.bind(folder, recorder)
            if resume is not None:
                descriptor = getattr(self.environment, 'continuation', {}).get('binding', {})
                selected = resume['selected_task'] or {}
                if descriptor.get('verified') is not True or any(
                    (
                        descriptor.get(k) != selected.get(k)
                        for k in ('task_id', 'task_instance_id', 'profile_id')
                    )
                ):
                    raise ValueError(
                        'Trusted restored task/world/profile binding is missing or mismatched'
                    )
                instance = recorder.instances.get(selected.get('task_instance_id'), {})
                expected = {
                    'server_instance_id': instance.get('server_instance_id'),
                    'workspace_world_key': instance.get('workspace_world_key'),
                }
                if any(
                    (
                        value is not None and descriptor.get(key) != value
                        for key, value in expected.items()
                    )
                ):
                    raise ValueError(
                        'Restored physical world differs from the saved task instance'
                    )
                if task_stream is not None and (
                    task_stream.current_id != selected.get('task_id')
                    or task_stream.position != resume['report']['task_stream']['position']
                ):
                    raise ValueError('Restored task stream moved past the interrupted task')
                self.policy.memory = resume['working_memory']
                if hasattr(self.policy, 'memory_file'):
                    save_json(
                        self.policy.memory_file,
                        {
                            'source': 'continued_agent_written',
                            'text': self.policy.memory,
                            'source_run_id': resume['source_run_id'],
                        },
                    )
                recorder.event(
                    'continuation.start',
                    **result['continuation'],
                    carried_evaluation_counts=copy.deepcopy(
                        resume['report'].get('evaluation_counts', {})
                    ),
                    carried_evaluations=len(result['evaluator_results']),
                    carried_evaluation_requests=resume['report'].get('evaluation_rounds', 0),
                    passed_task_ids=sorted(
                        {
                            r['task_id']
                            for r in result['evaluator_results']
                            if r['verdict'] == 'pass'
                        }
                    ),
                )
            auditor = self.auditor_factory(folder)
            config['audit_configuration'] = getattr(auditor, 'configuration', {})
            save_json(folder / 'config.json', config)
            if task_stream is not None:
                save_json(folder / 'task_stream_manifest.json', task_stream.manifest)
                config['task_delivery'] = {
                    'protocol': 'sequential_hidden_v1',
                    'manifest_file': 'task_stream_manifest.json',
                    'advance_on': 'verified_pass_or_explicit_model_abandonment',
                }
                result['task_stream'] = task_stream.public_state()
                save_json(folder / 'task_stream_progress.json', result['task_stream'])
            # Discover public interfaces, available actions, and currently visible tasks.
            discover(('environment.describe', 'actions.list', 'tasks.list'))
            # Enter the task-episode loop with this recorder bound to model calls.
            with observe_model_calls(recorder):
                stop = False
                while not stop:
                    if (
                        self.max_rounds is not None
                        and len(recorder.rounds) >= self.max_rounds
                    ):
                        break
                    control.check(recorder)
                    if task_stream is not None and task_stream.current_id is None:
                        result['status'] = 'task_stream_complete'
                        break
                    if recorder.remaining < 16:
                        result['status'] = 'token_budget_exhausted'
                        break
                    passed_ids = {
                        e['task_id']
                        for e in result['evaluator_results']
                        if e['verdict'] == 'pass'
                    }
                    progress = getattr(self.environment, 'task_progress', {})
                    passed_ids.update(
                        (
                            k
                            for k, v in progress.items()
                            if isinstance(v, dict) and v.get('state') == 'passed'
                        )
                    )
                    if task_stream is None and catalogue_ids and (catalogue_ids <= passed_ids):
                        result['status'] = 'catalogue_complete'
                        break
                    continuing_round = resume is not None and recorder.round is not None
                    if not continuing_round:
                        recorder.begin_round()
                        recorder.active_task = None
                    selected_task = (
                        copy.deepcopy(recorder.active_task) if continuing_round else None
                    )
                    selected_start_ids = {
                        c['request']['request_id']
                        for c in recorder.env_calls
                        if c['request']['op'] == 'task.start'
                        and isinstance(c['feedback'].get('result'), dict)
                        and (
                            c['feedback']['result'].get('id')
                            == (selected_task or {}).get('task_instance_id')
                        )
                    }
                    reason = None
                    transition = None
                    task_claim = None
                    previous = recorder.rounds[-1] if recorder.rounds else None
                    context['episode'] = {
                        'number': recorder.round['round'],
                        'phase': 'select_task',
                        'experiment_continues': True,
                        'passed_task_ids': sorted(passed_ids),
                        'previous': (
                            None
                            if previous is None
                            else {
                                'number': previous['round'],
                                'end_reason': previous['end_reason'],
                                'task': copy.deepcopy(
                                    previous.get('selected_task') or previous.get('final_task')
                                ),
                            }
                        ),
                    }
                    while reason is None:
                        control.check(recorder)
                        context['limits'] = {**result['limits'], 'max_rounds': control.cap}
                        context['round'] = recorder.round['round']
                        context['tokens_remaining'] = {
                            'run': recorder.token_budget - recorder.charged,
                            'round': (
                                recorder.remaining
                                if recorder.round_token_budget is not None
                                else None
                            ),
                        }
                        context['active_task'] = copy.deepcopy(
                            recorder.instances.get(
                                (recorder.active_task or {}).get('task_instance_id'), {}
                            ).get('task')
                        )
                        context['episode']['phase'] = (
                            'working' if selected_task else 'select_task'
                        )
                        context['task_progress'] = copy.deepcopy(
                            getattr(self.environment, 'task_progress', {})
                        )
                        if task_stream is not None:
                            context['task_stream'] = task_stream.public_state()
                        if hasattr(self.environment, 'task_workspace_status'):
                            # state: dict containing the host's current workspace and
                            # pending-transition fields.
                            state = self.environment.task_workspace_status()
                            active = state.get('active') or {}
                            context['task_workspace'] = {
                                k: state[k] for k in ('mode', 'active_task_id') if k in state
                            }
                            context['task_workspace']['active'] = {
                                k: active[k]
                                for k in (
                                    'task_id',
                                    'world_key',
                                    'instance_id',
                                    'activation_id',
                                )
                                if k in active
                            }
                            context['task_workspace']['pending'] = bool(state.get('pending'))
                        model_count = len(recorder.models)
                        replaying = bool(resume_pending)
                        try:
                            decision = (
                                {
                                    'decision': 'request',
                                    'request_json': json.dumps(resume_pending),
                                    'report': recorder.round['steps'][-1]['decision']['report'],
                                }
                                if replaying
                                else self.policy.decide(copy.deepcopy(context))
                            )
                        except ModelOutputError as exc:
                            error = {
                                'type': type(exc).__name__,
                                'message': str(exc),
                                'response_id': exc.response_id,
                            }
                            context['history'].append({'decision_error': error})
                            recorder.event(
                                'model.output_invalid',
                                error=error,
                                recovery='New decision in the same round; previous usage remains charged',
                            )
                            save_json(
                                folder / 'progress.json',
                                {**result, 'current_round': recorder.round},
                            )
                            continue
                        except TokenBudgetExceeded as exc:
                            if exc.limit != 'round':
                                raise
                            reason = 'round_token_budget_exhausted'
                            break
                        if (
                            not replaying
                            and len(recorder.models) == model_count
                            and (not getattr(self.policy, 'programmatic_continuation', False))
                        ):
                            raise RuntimeError(
                                'Policy bypassed instrumented model transport; token accounting unavailable'
                            )
                        if recorder.budget_blocked:
                            reason = budget_reason()
                            if recorder.budget_limit != 'round':
                                result['status'] = reason
                                stop = True
                            break
                        if replaying:
                            record = recorder.round['steps'][-1]
                            history = context['history'][-1]
                        else:
                            record = {
                                'decision_index': context.get('history_offset', 0)
                                + len(context['history'])
                                + 1,
                                'decision': decision,
                                'calls': [],
                            }
                            recorder.round['steps'].append(record)
                            history = {
                                'decision': {
                                    'decision': decision['decision'],
                                    'report': decision['report'],
                                },
                                'calls': [],
                            }
                            context['history'].append(history)
                        completed_prefix = len(record['calls'])
                        checkpoint()
                        if decision['decision'] == 'remember':
                            checkpoint()
                            continue
                        if decision['decision'] == 'finish':
                            result['model_claim'] = decision['report']
                            if task_stream is not None:
                                transition = 'failed'
                                task_claim = decision['report']
                            reason = 'model_finish'
                            break
                        try:
                            # requests: decoded JSON; normalize a single dict to a list before
                            # validating the request batch.
                            requests = json.loads(decision['request_json'])
                            if isinstance(requests, dict):
                                # requests changes from one request dict to list[dict].
                                requests = [requests]
                            if (
                                not isinstance(requests, list)
                                or not requests
                                or (not all((isinstance(r, dict) for r in requests)))
                            ):
                                raise ValueError(
                                    'Expected a request object or nonempty array of request objects'
                                )
                        except (ValueError, TypeError) as exc:
                            history['feedback'] = {
                                'status': 'error',
                                'error': {'type': 'MalformedRequest', 'message': str(exc)},
                            }
                            record['feedback'] = history['feedback']
                            continue
                        if not replaying:
                            record['planned_requests'] = len(requests)
                        # resume_pending: list[dict] of parent-segment requests not yet
                        # dispatched.
                        resume_pending = []
                        expanded = hasattr(self.policy, 'request_stream')
                        stream = (
                            self.policy.request_stream(requests, context)
                            if expanded
                            else iter(requests)
                        )
                        active_stream = stream
                        for request in stream:
                            control.check(recorder)
                            # view: dict retaining the provider-selected feedback separately
                            # from the full recorded feedback.
                            view = {}
                            if getattr(self.policy, 'compact_transport', False) and request.get(
                                'op'
                            ) in (
                                'action.execute',
                                'evaluation.run',
                                'evaluation.get',
                                'submission.validate',
                            ):
                                method = (
                                    request.get('params', {})
                                    .get('action', {})
                                    .get('method', '')
                                )
                                metadata = (
                                    self.environment.tool_metadata(method)
                                    if method and hasattr(self.environment, 'tool_metadata')
                                    else {}
                                )
                                read = is_read_only(metadata)
                                prefer_default = (
                                    self.compact_discovery
                                    and hasattr(self.environment, 'prefer_default_response')
                                    and self.environment.prefer_default_response(request)
                                )
                                if 'response' not in request and (not prefer_default):
                                    request = {
                                        **request,
                                        'response': {'detail': 'full' if read else 'summary'},
                                    }

                            def invoke(req):
                                """Dispatch a request, retain its selected view, and return
                                full feedback for the recorder.
                                """

                                if (
                                    task_stream is None
                                    and selected_task
                                    and (req.get('op') == 'task.start')
                                    and (req['request_id'] not in selected_start_ids)
                                ):
                                    return {
                                        'protocol_version': '1.0',
                                        'request_id': req['request_id'],
                                        'session_id': getattr(
                                            getattr(self.environment, 'session', None),
                                            'run_id',
                                            'unavailable',
                                        ),
                                        'status': 'error',
                                        'result': None,
                                        'error': {
                                            'type': 'TaskAlreadySelected',
                                            'message': 'This round already binds one task instance; use the environment task status interface before switching.',
                                        },
                                    }
                                selected = self.environment.handle(req)
                                view['feedback'] = selected
                                return (
                                    self.environment.raw_feedback(
                                        req.get('request_id'), request=req
                                    )
                                    or selected
                                    if hasattr(self.environment, 'raw_feedback')
                                    else selected
                                )

                            try:
                                feedback, call = recorder.observe_env(request, invoke)
                            except BaseException as exc:
                                interrupted = getattr(exc, 'recorded_environment_call', None)
                                if (
                                    isinstance(interrupted, dict)
                                    and interrupted.get('request') == request
                                    and any((c is interrupted for c in recorder.env_calls))
                                ):
                                    if interrupted['call_id'] not in record['calls']:
                                        record['calls'].append(interrupted['call_id'])
                                        history['calls'].append(
                                            {
                                                'request': copy.deepcopy(request),
                                                'feedback': copy.deepcopy(
                                                    interrupted['feedback']
                                                ),
                                            }
                                        )
                                    current = interrupted['feedback']
                                    if current.get('status') != 'completed':
                                        record['stop_reason'] = current.get('status')
                                    count = len(record['calls']) - completed_prefix
                                    record['unexecuted_requests'] = (
                                        None if expanded else len(requests) - count
                                    )
                                    if not expanded and requests[count:]:
                                        history['unexecuted_requests'] = copy.deepcopy(
                                            requests[count:]
                                        )
                                    else:
                                        history.pop('unexecuted_requests', None)
                                    checkpoint()
                                raise
                            if view.get('feedback') != feedback:
                                call['response_view'] = copy.deepcopy(
                                    view.get('feedback', feedback)
                                )
                            record['calls'].append(call['call_id'])
                            history['calls'].append(
                                {
                                    'request': copy.deepcopy(request),
                                    'feedback': copy.deepcopy(view.get('feedback', feedback)),
                                }
                            )
                            checkpoint()
                            if (
                                request.get('op') == 'task.start'
                                and feedback.get('infrastructure', {}).get('status')
                                == 'request_uncertain'
                            ):
                                raise RunStopped(
                                    'task_workspace_transition_uncertain',
                                    'Task activation is uncertain; preserve its original host receipt and world identity before continuing',
                                )
                            if request.get('op') == 'task.start' and operation_succeeded(
                                feedback
                            ):
                                selected_start_ids.add(request['request_id'])
                                selected_task = copy.deepcopy(recorder.active_task)
                                recorder.round['selected_task'] = copy.deepcopy(selected_task)
                            if (
                                request.get('op') == 'task.progress'
                                and operation_succeeded(feedback)
                                and (
                                    selected_task is None
                                    or request['params']['task_id'] == selected_task['task_id']
                                )
                                and (
                                    task_stream is None
                                    or request['params']['task_id'] == task_stream.current_id
                                )
                            ):
                                # state is now str, the requested task-progress state such as
                                # deferred or abandoned.
                                state = request['params']['state']
                                if state in ('deferred', 'abandoned'):
                                    if task_stream is not None:
                                        transition = (
                                            'deferred' if state == 'deferred' else 'failed'
                                        )
                                    task_claim = request['params']['reason']
                                    reason = 'task_' + state
                            if hasattr(self.policy, 'on_feedback'):
                                self.policy.on_feedback(request, feedback)
                            # evaluation: a JSON value; a successful evaluation.run result is
                            # checked against evaluation-report below.
                            evaluation = feedback.get('result')
                            if request.get('op') == 'evaluation.run' and operation_succeeded(
                                feedback
                            ):
                                validate('evaluation-report', evaluation)
                                if selected_task and any(
                                    (
                                        evaluation.get(key) != selected_task[key]
                                        for key in ('task_id', 'task_instance_id')
                                    )
                                ):
                                    raise ValueError(
                                        'Evaluation report does not match this round task instance'
                                    )
                                if evaluation['evaluation_id'] not in evaluation_ids:
                                    result['evaluator_results'].append(evaluation)
                                    evaluation_ids.add(evaluation['evaluation_id'])
                                    if (
                                        request.get('op') == 'evaluation.run'
                                        and evaluation['verdict'] == 'pass'
                                        and (
                                            selected_task is None
                                            or evaluation['task_instance_id']
                                            == selected_task['task_instance_id']
                                        )
                                    ):
                                        if (
                                            task_stream is not None
                                            and evaluation['task_id'] == task_stream.current_id
                                        ):
                                            transition = 'pass'
                                        reason = 'task_passed'
                            boundary = request.get('request_id')
                            if not isinstance(boundary, str):
                                # boundary becomes the recorded call ID when the request has no
                                # string request_id.
                                boundary = call['call_id']
                            if (
                                request.get('op') == 'evaluation.run'
                                and boundary not in boundary_ids
                            ):
                                boundary_ids.add(boundary)
                            if recorder.budget_blocked:
                                reason = reason or budget_reason()
                                if recorder.budget_limit != 'round':
                                    result['status'] = budget_reason()
                                    stop = True
                            if (
                                reason
                                or request.get('op') == 'evaluation.run'
                                or (not operation_succeeded(feedback))
                            ):
                                record.setdefault('stop_reason', feedback.get('status'))
                                break
                            if request.get('op') == 'session.close':
                                result['status'] = 'session_closed'
                                reason = 'session_closed'
                                stop = True
                                break
                        if hasattr(stream, 'close'):
                            stream.close()
                        # active_stream: Iterator[dict] | None, retained for cleanup if request
                        # dispatch is interrupted.
                        active_stream = None
                        if expanded:
                            record['expanded_requests'] = len(record['calls'])
                            history['method_feedback'] = copy.deepcopy(self.policy.feedback)
                        record['unexecuted_requests'] = (
                            None
                            if expanded
                            else len(requests) - (len(record['calls']) - completed_prefix)
                        )
                        if record['unexecuted_requests']:
                            history['unexecuted_requests'] = copy.deepcopy(
                                requests[len(record['calls']) - completed_prefix :]
                            )
                        else:
                            history.pop('unexecuted_requests', None)
                        save_json(
                            folder / 'progress.json',
                            {**result, 'current_round': recorder.round},
                        )
                    close_round(reason)
                    if transition is not None:
                        complete = advance_task(transition, task_claim)
                        if complete and (not stop):
                            result['status'] = 'task_stream_complete'
                        stop = stop or complete
                        if not stop:
                            control.check(recorder)
                            discover(('environment.describe', 'tasks.list'))
            # Finalize the run status after the task loops end.
            if result['status'] == 'running':
                result['status'] = 'round_budget_exhausted'
        except _TaskWorkspaceReleaseError:
            pass
        except RunStopped as exc:
            result['status'] = exc.reason
            result['control_notice'] = str(exc)
            recorder.event(
                'operator.stop', reason=exc.reason, message=str(exc), control=control.snapshot()
            )
        except TokenBudgetExceeded:
            result['status'] = budget_reason()
        except KeyboardInterrupt:
            result['status'] = 'interrupted'
        except Exception as exc:
            if result['status'] != 'task_workspace_release_failed':
                result['status'] = (
                    'accounting_blocked' if recorder.budget_limit == 'accounting' else 'failed'
                )
                result['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        finally:
            try:
                checkpoint()
            except Exception as exc:
                result.setdefault('finalization_errors', []).append(
                    {
                        'stage': 'continuation.checkpoint',
                        'type': type(exc).__name__,
                        'message': str(exc),
                    }
                )
                if not has_primary_failure():
                    result['status'] = 'checkpoint_failed'
            if active_stream is not None and hasattr(active_stream, 'close'):
                try:
                    active_stream.close()
                except RunStopped:
                    pass
                except Exception as exc:
                    result['stream_cleanup_error'] = {
                        'type': type(exc).__name__,
                        'message': str(exc),
                    }
            if recorder.round is not None:
                try:
                    close_round(result['status'])
                except Exception as exc:
                    result.setdefault('finalization_errors', []).append(
                        {
                            'stage': 'round.finalize',
                            'type': type(exc).__name__,
                            'message': str(exc),
                        }
                    )
                    if not has_primary_failure() and result['status'] not in (
                        'task_workspace_release_failed',
                        'failed',
                    ):
                        result['status'] = 'round_finalization_failed'
            started = time.perf_counter()
            try:
                result['cleanup'] = self.environment.close()
            except Exception as exc:
                result['cleanup'] = {'closed': False, 'cleanup_errors': [str(exc)]}
                if not has_primary_failure():
                    result['status'] = 'cleanup_failed'
            result['cleanup_wall_seconds'] = time.perf_counter() - started
            result['operator_control'] = control.snapshot()
            if task_stream is not None:
                result['task_stream'] = task_stream.public_state()
                save_json(folder / 'task_stream_progress.json', result['task_stream'])
            result.update(
                finished_at=utc_now(),
                wall_seconds=time.perf_counter() - recorder.started,
                audit_wall_seconds=audit_seconds,
                token_usage=summarize_usage(recorder.models),
                model_calls=len(recorder.models),
                environment_requests=sum(
                    (c['stage'] == 'workflow' for c in recorder.env_calls)
                ),
            )
            if resume is not None:
                result['segment_token_usage'] = summarize_usage(
                    recorder.models[carried_models:]
                )
                result['segment_model_calls'] = len(recorder.models) - carried_models
                result['segment_charged_tokens'] = recorder.charged - carried_tokens
            result['workflow_wall_seconds'] = sum((r['wall_seconds'] for r in recorder.rounds))
            result['discovery_wall_seconds'] = sum(
                (c['wall_seconds'] for c in recorder.env_calls if c['stage'] == 'discovery')
            )
            result['evaluation_counts'] = {
                v: sum((r['verdict'] == v for r in result['evaluator_results']))
                for v in ('pass', 'fail', 'inconclusive', 'measured')
            }
            result['task_successes'] = len(
                {
                    r['task_instance_id']
                    for r in result['evaluator_results']
                    if r['verdict'] == 'pass'
                }
            )
            passed = [r for r in result['evaluator_results'] if r['verdict'] == 'pass']
            result['distinct_tasks_passed'] = len({r['task_id'] for r in passed})
            profiles = {
                (
                    r['task_id'],
                    recorder.instances.get(r['task_instance_id'], {})
                    .get('task', {})
                    .get('selected_profile'),
                )
                for r in passed
            }
            result['distinct_profiles_passed'] = len(
                {(task, profile) for task, profile in profiles if profile is not None}
            )
            result['profile_results_complete'] = all(
                (profile is not None for task, profile in profiles)
            )
            result['open_measurements'] = result['evaluation_counts']['measured']
            result['evaluation_rounds'] = len(boundary_ids)
            result['partial_rounds'] = sum(
                (
                    r['end_reason']
                    not in (
                        'task_passed',
                        'task_deferred',
                        'task_abandoned',
                        'model_task_abandoned',
                    )
                    for r in recorder.rounds
                )
            )
            try:
                result['environment_artifacts'] = archive_environment(
                    folder, self.environment, result['evaluator_results']
                )
            except Exception as exc:
                result['artifact_export_error'] = {
                    'type': type(exc).__name__,
                    'message': str(exc),
                }
            if hasattr(self.policy, 'summary'):
                try:
                    result['method'] = self.policy.summary()
                except Exception as exc:
                    result['method_summary_error'] = {
                        'type': type(exc).__name__,
                        'message': str(exc),
                    }
            try:
                recorder.event(
                    'run.finish',
                    status=result['status'],
                    token_usage=result['token_usage'],
                    error=result.get('error'),
                )
            except Exception as exc:
                result['event_log_error'] = {
                    'stage': 'run.finish',
                    'type': type(exc).__name__,
                    'message': str(exc),
                }
            from runtime.reports import finalize_report

            finalize_report(folder, result, render=write_html)
        return result


from runtime.reports import write_html
from runtime.evidence import archive_environment
