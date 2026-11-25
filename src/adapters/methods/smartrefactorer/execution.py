"""Exchange candidate requests and feedback, run fixtures, and record invocation outcomes."""

import json
from jsonschema import Draft202012Validator, ValidationError, SchemaError
from algorithms.smartrefactorer.execution.bindings import resolve_arguments
import copy
import queue
import uuid
from runtime.contracts import validate
from runtime.feedback_semantics import execution_state
from audit.control import RunStopped
from algorithms.smartrefactorer.library import digest
from algorithms.smartrefactorer.execution import python_backend
from algorithms.smartrefactorer.execution.legacy_step import step
from algorithms.smartrefactorer.execution.executor import Program, snapshot


class ExecutionBridge:
    """Connect a candidate Program to the runner's public request loop.

    controller stores the candidate and execution records. recorder receives event
    history, environment handles public calls, and directory is the method-state
    Path used for generated execution files. persist and guard are the run callbacks.

    program is the attached Program or None. program_stream() consumes its events until a
    request or final outcome is reached. A request is saved in the checkpoint and
    yielded to the runner. on_feedback() matches its receipt and delivers the result
    to the program or a saved instruction frame. feedback contains adapter notices
    for the current decision.

    run_fixture() follows the same event exchange using recorded request feedback.
    An accepted task evaluation passes its receipt to the controller for publication.

    Example, after development has checked the candidate::

        bridge = adapter.execution
        bridge.start_program(component_id, arguments)
        for request in bridge.program_stream():
            feedback = environment.handle(request)
            bridge.on_feedback(request, feedback)

    This handles the next request boundary. The runner records the exchange and
    enters the stream again to continue the invocation or collect its final outcome.
    """

    def __init__(self, controller, recorder, environment, directory, save, guard):
        """Keep the controller, run services, and execution directory."""

        self.controller = controller
        self.recorder = recorder
        self.environment = environment
        self.directory = directory
        self.persist = save
        self.guard = guard
        self.program = None
        self.feedback = []

    def run_fixture(self, components, component_id, arguments, receipts=()):
        """Return the Python result and consumed recorded interactions from a fixture run."""
        # Fixtures record requests, results, and exceptions. Skip call/local
        # snapshots to avoid copying growing mutable mocks at every trace event.
        program = Program(
            components,
            component_id,
            arguments,
            self.directory / 'fixture-executions' / str(uuid.uuid4()),
            trace_calls=False,
        ).start()
        observed = []
        cursor = 0
        try:
            while True:
                self.guard()
                try:
                    event = program.events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event['kind'] == 'request':
                    if cursor >= len(receipts):
                        raise python_backend.FixtureEvidenceMissing(
                            'Missing recorded feedback for ' + repr(event['request'])
                        )
                    evidence = receipts[cursor]
                    cursor += 1
                    if event['request'] != evidence['request']:
                        raise ValueError('Fixture request mismatch: ' + repr(event['request']))
                    observed.append(copy.deepcopy(evidence))
                    program.deliver(evidence['feedback'])
                elif event['kind'] == 'exception':
                    raise python_backend.FixtureExecutionError(event['error'])
                elif event['kind'] == 'completed':
                    if cursor != len(receipts):
                        raise ValueError('Fixture contains unconsumed request evidence')
                    return {'result': program.result, 'requests': observed}
        finally:
            program.cancel()
            program.thread.join(timeout=0.5)

    def program_stream(self):
        """Yield the next Program request, or record its final outcome."""
        # state: CheckpointView, the dictionary linked to the controller's AlgorithmState.
        state = self.controller.s
        if self.program is None:
            raise RunStopped(
                'operator_stop',
                'Execution process is unavailable; retain facts for an explicit continuation decision',
            )
        while True:
            self.guard()
            try:
                # event: dict with a kind tag; request, completed, and exception carry different
                # payloads.
                event = self.program.events.get(timeout=0.1)
            except queue.Empty:
                continue
            event['invocation_id'] = self.program.invocation_id
            self.recorder.event('smartrefactorer.program.event', record=copy.deepcopy(event))
            if event['kind'] == 'request':
                request = copy.deepcopy(event['request'])
                request.setdefault('protocol_version', '1.0')
                request.setdefault('request_id', str(uuid.uuid4()))
                # Forward public calls through the host's operation admission
                # and lifecycle rules.
                state['pending'] = {
                    'request': request,
                    'candidate_hash': state['executed_candidate_hash'],
                    'program_invocation_id': self.program.invocation_id,
                }
                state['python_execution']['status'] = 'waiting'
                self.persist()
                yield request
                return
            state.setdefault('program_history', []).append(event)
            if event['kind'] == 'completed':
                state['python_execution']['status'] = 'completed'
                state['last_execution'] = {
                    'execution': 'completed',
                    'result': event['result'],
                    'candidate_hash': state['executed_candidate_hash'],
                }
                state['awaiting_task_verdict'] = True
                self.program = None
                self.persist()
                return
            if event['kind'] == 'exception':
                state['python_execution']['status'] = 'failed'
                state['failure'] = copy.deepcopy(event['error'])
                state['execution_recovery'] = copy.deepcopy(state['python_execution'])
                state['phase'] = 'repair'
                self.program = None
                self.persist()
                return
            self.persist()

    def request_stream(self, requests, context):
        """Expand execution-driving decisions and yield their public requests."""
        s = self.controller.s
        for request in requests:
            if request.get('op') == 'smartrefactorer.execute':
                yield from self.program_stream()
                continue
            if request.get('op') != 'smartrefactorer.step':
                request = copy.deepcopy(request)
                request.setdefault('protocol_version', '1.0')
                request.setdefault('request_id', str(uuid.uuid4()))
                if (
                    request.get('op') == 'action.execute'
                    and self.environment is not None
                    and (not self.environment.readonly(request))
                ):
                    rejection = {
                        'request': copy.deepcopy(request),
                        'status': 'not_started',
                        'error': {
                            'type': 'SkillExecutionRequired',
                            'message': 'Task mutations must originate from the validated task entry with its durable cursor; coordinator requests are public reads and lifecycle operations.',
                        },
                    }
                    self.feedback.append(rejection)
                    self.recorder.event(
                        'smartrefactorer.coordinator.request_rejected', rejection=rejection
                    )
                    continue
                yield request
                continue
            if s.get('pending'):
                if s['pending'].get('receipt') is not None or s['pending'].get(
                    'receipt_conflict'
                ):
                    raise RunStopped(
                        'pending_request_unresolved',
                        'Receipt requires reconciliation; never replay automatically',
                    )
                if not s['pending'].pop('dispatch_permitted', False):
                    raise RunStopped(
                        'pending_request_unresolved',
                        'No verified unsent-request admission; retain pending request instead of replaying it',
                    )
                self.persist()
                yield copy.deepcopy(s['pending']['request'])
                return
            for _ in range(128):
                self.guard()
                if not s['frames']:
                    return
                frame = s['frames'][-1]
                c = s['candidate'][frame['component_id']]
                try:
                    transition = step(
                        c['source'], frame['state'], frame['feedback'], frame['args']
                    )
                    if 'call' in transition:
                        if transition['call'] not in s['candidate']:
                            raise ValueError('Unknown helper ' + transition['call'])
                        if transition['call'] not in c.get('dependencies', []):
                            raise ValueError(
                                'Helper call is absent from the declared source dependency graph'
                            )
                        if len(s['frames']) >= 64:
                            raise ValueError('Invocation depth exceeded')
                        from jsonschema import (
                            Draft202012Validator,
                            ValidationError,
                            SchemaError,
                        )

                        try:
                            schema = (
                                s['candidate'][transition['call']]
                                .get('interface', {})
                                .get('args_schema', {})
                            )
                            Draft202012Validator.check_schema(schema)
                            Draft202012Validator(schema).validate(transition.get('args', {}))
                        except (ValidationError, SchemaError) as exc:
                            raise ValueError(
                                'Helper arguments do not match its declared interface: '
                                + str(exc)
                            ) from exc
                    if 'request' in transition and transition['request'].get('op') in (
                        'task.start',
                        'evaluation.run',
                        'session.close',
                        'submission.validate',
                        'task.progress',
                    ):
                        raise ValueError('Skills cannot own task lifecycle or acceptance')
                except (
                    ValueError,
                    KeyError,
                    TypeError,
                    IndexError,
                    ArithmeticError,
                    SyntaxError,
                    RecursionError,
                ) as exc:
                    self.controller.record_execution_failure(
                        {
                            'type': type(exc).__name__,
                            'message': str(exc),
                            'frame': copy.deepcopy(frame),
                        }
                    )
                    s['suspended_execution'] = {
                        'frames': copy.deepcopy(s['frames']),
                        'candidate_hash': s.get('executed_candidate_hash'),
                        'failed_source_hash': c['hash'],
                        'source_before': c['source'],
                    }
                    s['frames'] = []
                    self.persist()
                    return
                frame['state'] = transition['state']
                frame['feedback'] = None
                if 'done' in transition:
                    s['frames'].pop()
                    self.recorder.event(
                        'smartrefactorer.invocation.end',
                        frame=frame,
                        result=transition.get('result'),
                    )
                    if s['frames']:
                        s['frames'][-1]['feedback'] = {'result': transition.get('result')}
                    else:
                        s['last_execution'] = {
                            'candidate_hash': s.get('executed_candidate_hash'),
                            'result': transition.get('result'),
                            'execution': 'completed',
                        }
                        s['awaiting_task_verdict'] = True
                elif 'call' in transition:
                    name = transition['call']
                    child = {
                        'component_id': name,
                        'args': transition.get('args', {}),
                        'state': {},
                        'feedback': None,
                        'invocation_id': str(uuid.uuid4()),
                        'parent_id': frame['invocation_id'],
                    }
                    s['frames'].append(child)
                    self.recorder.event('smartrefactorer.invocation.start', frame=child)
                else:
                    req = copy.deepcopy(transition['request'])
                    req.setdefault('protocol_version', '1.0')
                    req.setdefault('request_id', str(uuid.uuid4()))
                    s['pending'] = {
                        'request': req,
                        'candidate_hash': s['executed_candidate_hash'],
                        'invocation_id': frame['invocation_id'],
                    }
                    self.persist()
                    yield req
                    return
                self.persist()
            raise RunStopped(
                'skill_step_limit',
                'Pure transitions made no external request; retain cursor for diagnosis',
            )

    def on_feedback(self, request, feedback):
        """Match a receipt, advance its waiting invocation, and process task evaluations."""
        s = self.controller.s
        pending = s.get('pending')
        if (
            pending
            and pending['request'].get('request_id') == request.get('request_id')
            and (pending['request'] != request)
        ):
            pending['receipt_conflict'] = {
                'request': copy.deepcopy(request),
                'feedback': copy.deepcopy(feedback),
            }
            self.persist()
            raise RunStopped(
                'pending_request_unresolved',
                'Receipt request payload differs from the pending operation; no replay or cursor advance',
            )
        if pending and pending['request'] == request:
            s['last_execution'] = {
                'request': copy.deepcopy(request),
                'receipt': copy.deepcopy(feedback),
                'candidate_hash': pending['candidate_hash'],
            }
            pending['receipt'] = copy.deepcopy(feedback)
            self.persist()
            try:
                # state: str | None, the operation execution status extracted from this receipt.
                state = execution_state(feedback, request=request)
            except ValueError as exc:
                raise RunStopped(
                    'pending_request_unresolved',
                    'Invalid canonical receipt; retain original evidence without guessing execution state',
                ) from exc
            if request.get('op') == 'action.execute' and state is None:
                raise RunStopped(
                    'pending_request_unresolved',
                    'Native receipt has no execution metadata; preserve evidence and require producer reconciliation',
                )
            if state in ('unknown', 'running') or feedback.get('status') in (
                'pending_or_unknown',
                'running',
            ):
                pending['receipt'] = copy.deepcopy(feedback)
                self.persist()
                raise RunStopped(
                    'pending_request_unresolved' if state != 'running' else 'operation_running',
                    'Retain original request and receipt; no automatic replay',
                )
            if pending.get('program_invocation_id'):
                if self.program is not None:
                    self.program.deliver(feedback)
                    s['python_execution']['status'] = 'running'
                else:
                    s['python_execution']['status'] = 'interrupted'
                    s['execution_recovery'] = copy.deepcopy(s['python_execution'])
                s['pending'] = None
            else:
                s['frames'][-1]['feedback'] = copy.deepcopy(feedback)
                s['pending'] = None
            if feedback.get('status') != 'completed':
                s['last_operation_error'] = copy.deepcopy(s['last_execution'])
            self.persist()
        if request.get('op') == 'evaluation.run' and feedback.get('status') == 'completed':
            receipt = feedback['result']
            validate('evaluation-report', receipt)
            binding = s.get('binding') or {}
            if any((receipt.get(k) != binding.get(k) for k in ('task_id', 'task_instance_id'))):
                raise ValueError('Foreign evaluator receipt')
            s.setdefault('evaluations', []).append(copy.deepcopy(receipt))
            self.persist()
            if (
                receipt['verdict'] == 'pass'
                and s.get('executed_candidate_hash') == digest(s['candidate'])
                and (not s['frames'])
                and (not s['pending'])
            ):
                try:
                    self.controller.accepted(
                        {
                            **receipt,
                            'candidate_hash': s['executed_candidate_hash'],
                            'evaluation_request': copy.deepcopy(request),
                            'binding': copy.deepcopy(binding),
                            'execution_segments': copy.deepcopy(
                                s.get('execution_segments', [])
                            ),
                        }
                    )
                except (ValueError, OSError) as exc:
                    s['publication_error'] = {'type': type(exc).__name__, 'message': str(exc)}
                self.persist()
            elif receipt['verdict'] != 'pass':
                self.controller.record_task_failure(receipt)
                self.persist()

    def on_round_end(self, record):
        """Cancel the live wait and save the interrupted invocation and pending request."""
        if self.program is not None:
            self.program.cancel()
            self.controller.s['python_execution']['status'] = 'interrupted'
            self.controller.s['execution_recovery'] = copy.deepcopy(
                self.controller.s['python_execution']
            )
            self.controller.s['execution_recovery']['pending_request'] = copy.deepcopy(
                self.controller.s.get('pending')
            )
        self.persist()

    def drive_decision(self, legacy=False, report=None):
        """Build the internal request used to advance the selected execution backend."""

        operation = 'smartrefactorer.step' if legacy else 'smartrefactorer.execute'
        return {
            'decision': 'request',
            'request_json': json.dumps({'op': operation, 'params': {}}),
            'report': report
            or (
                'Continue saved legacy cursor'
                if legacy
                else 'Continue the live Python invocation'
            ),
        }

    def start_program(self, component_id, arguments, continuation_of=None, invocation_id=None):
        """Record a checked invocation, save it, and start its Python worker."""
        if self.program is not None:
            raise RunStopped('pending_request_unresolved', 'An invocation is already alive')
        invocation_id = invocation_id or str(uuid.uuid4())
        execution = self.controller.prepare_invocation(
            component_id, arguments, invocation_id, continuation_of
        )
        self.persist()
        self.program = Program(
            self.controller.state.candidate,
            component_id,
            arguments,
            self.directory / 'executions' / invocation_id,
            invocation_id,
        ).start()
        self.recorder.event('smartrefactorer.program.start', execution=copy.deepcopy(execution))

    def start_entry(self, entry, public, read_evidence):
        """Resolve task-entry arguments and initialize the component's execution backend."""
        state = self.controller.state
        component_id = entry['component_id']
        if state.candidate[component_id].get('language') == python_backend.PROFILE:
            self.controller.preserve_legacy_interruption()
            if state.execution.recovery:
                self.persist()
                return {
                    'decision': 'remember',
                    'request_json': '',
                    'report': 'Candidate changes saved. Retained execution facts require an explicit continuation invocation.',
                }
            invocation_id = str(uuid.uuid4())
            # arguments: JSON value after replacing the task entry's public and evidence
            # references.
            arguments = resolve_arguments(
                entry.get('argument_bindings', {}),
                {
                    **public,
                    'invocation': {'id': invocation_id, 'request_namespace': invocation_id},
                },
                read_evidence,
            )
            self.start_program(component_id, arguments, invocation_id=invocation_id)
            return self.drive_decision()
        execution = state.execution
        suspended = execution.suspended_legacy
        if suspended:
            frames = suspended['frames']
            if any((frame['component_id'] not in state.candidate for frame in frames)):
                self.controller.s['continuation_blocker'] = {
                    'kind': 'missing_component',
                    'frames': copy.deepcopy(frames),
                }
                self.persist()
                raise RunStopped(
                    'operator_stop',
                    'Retain the suspended cursor; a referenced component is absent',
                )
            try:
                for frame in frames:
                    schema = (
                        state.candidate[frame['component_id']]
                        .get('interface', {})
                        .get('args_schema', {})
                    )
                    Draft202012Validator.check_schema(schema)
                    Draft202012Validator(schema).validate(frame['args'])
            except (ValidationError, SchemaError) as error:
                self.controller.s['continuation_blocker'] = {
                    'kind': 'saved_arguments_incompatible',
                    'message': str(error),
                }
                self.persist()
                raise RunStopped(
                    'operator_stop',
                    'Retain the saved invocation; patched arguments are incompatible',
                ) from error
            execution.legacy_frames = copy.deepcopy(frames)
            execution.version_segments.append(
                {
                    'prior_candidate_hash': suspended['candidate_hash'],
                    'resumed_candidate_hash': digest(state.candidate),
                    'invocation_ids': [frame['invocation_id'] for frame in frames],
                }
            )
            execution.suspended_legacy = None
        else:
            invocation_id = str(uuid.uuid4())
            # arguments: JSON value after replacing the task entry's public and evidence
            # references.
            arguments = resolve_arguments(
                entry.get('argument_bindings', {}),
                {
                    **public,
                    'invocation': {'id': invocation_id, 'request_namespace': invocation_id},
                },
                read_evidence,
            )
            schema = state.candidate[component_id].get('interface', {}).get('args_schema', {})
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(arguments)
            execution.legacy_frames = [
                {
                    'component_id': component_id,
                    'args': arguments,
                    'state': {},
                    'feedback': None,
                    'invocation_id': invocation_id,
                    'parent_id': None,
                }
            ]
        execution.invocation = None
        execution.executed_candidate_hash = digest(state.candidate)
        self.persist()
        return self.drive_decision(legacy=True)
