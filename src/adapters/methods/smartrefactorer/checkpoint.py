"""Explicit SmartRefactorer continuation from complete, clean stopped evidence.

No environment calls, model calls, source mutation, or guessed world recovery.
"""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
from runtime.continuation.safety import clean_discovery_only_failure
from runtime.context.codec import decode
from runtime.feedback_semantics import is_uncertain


def load_continuation(source, *, readonly=None):
    source = Path(source).resolve()
    raw = (source / 'report.json').read_bytes()
    report = json.loads(raw)
    method = report.get('method') or {}
    development = source / 'method/state/smartrefactorer/development.json'
    if report.get('method_summary_error'):
        raise ValueError('Method checkpoint summary failed; preserve source for repair')
    expected = method.get('checkpoint_sha256')
    if method.get('implementation_revision') in ('v14', 'refactor-v1') and not expected:
        raise ValueError('Missing method checkpoint hash')
    if expected and (
        not development.is_file()
        or hashlib.sha256(development.read_bytes()).hexdigest() != expected
    ):
        raise ValueError('Method checkpoint differs from the finalized report')
    if method.get('embedding_cache_sha256'):
        cache = source / 'method/state/smartrefactorer/embedding-cache.json'
        if (
            not cache.exists()
            or hashlib.sha256(cache.read_bytes()).hexdigest()
            != method['embedding_cache_sha256']
        ):
            raise ValueError('Embedding cache differs from finalized report')
    if method.get('library_manifest_hash'):
        manifest = source / 'method/state/smartrefactorer/library/manifest.json'
        value = (
            json.loads(manifest.read_text(encoding='utf-8'))
            if manifest.exists()
            else {'version': 0, 'components': {}, 'commits': []}
        )
        from algorithms.smartrefactorer.library import digest

        if digest(value) != method['library_manifest_hash']:
            raise ValueError('Library manifest differs from the finalized report')
    discovery_failure = clean_discovery_only_failure(source, report)
    if report.get('workflow') not in ('smartrefactorer', 'smartrefactorer_compare') or (
        report.get('status')
        not in (
            'operator_stop',
            'interrupted',
            'model_context_blocked',
            'source_revision_changed',
            'runtime_revision_changed',
        )
        and not discovery_failure
    ):
        raise ValueError(
            'SmartRefactorer continuation requires an explicitly stopped SmartRefactorer report'
        )
    from .resume_accounting import validate_resume_accounting, collect_resume_model_calls

    validate_resume_accounting(source, report)
    rounds = copy.deepcopy(report['rounds'])
    if not rounds or (
        rounds[-1].get('end_reason')
        not in (
            'operator_stop',
            'interrupted',
            'model_context_blocked',
            'source_revision_changed',
            'runtime_revision_changed',
        )
        and not (discovery_failure and rounds[-1].get('end_reason') == 'failed')
    ):
        raise ValueError('No interrupted task round to continue')
    models = []
    calls = []
    seen_models = set()
    seen_calls = set()
    for record in rounds:
        for field, target, seen in (
            ('model_calls', models, seen_models),
            ('env_calls', calls, seen_calls),
        ):
            for call in record.get(field, []):
                if call['call_id'] not in seen:
                    target.append(copy.deepcopy(call))
                    seen.add(call['call_id'])
    models = copy.deepcopy(collect_resume_model_calls(source, report))
    if (
        sum(c.get('charged_tokens', 0) for c in models)
        != report['token_usage']['charged_tokens']
    ):
        raise ValueError('Report model-call evidence does not reconcile with charged usage')
    starts = set()
    finishes = set()
    env_starts = set()
    env_finishes = set()
    env_request_ids = set()
    with (source / 'log/events.jsonl').open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                raise ValueError('Incomplete stopped event ledger')
            event = json.loads(line)
            kind = event.get('kind')
            call = event.get('call') or {}
            rid = call.get('call_id')
            if kind == 'model.start':
                starts.add(rid)
            if kind in ('model.finish', 'model.not_sent'):
                finishes.add(rid)
            if kind == 'env.start':
                env_starts.add(rid)
                env_request_ids.add((call.get('request') or {}).get('request_id'))
            if kind == 'env.finish':
                env_finishes.add(rid)
    if starts - finishes or env_starts - env_finishes:
        raise ValueError('In-flight request retained; continuation cannot replay it')
    for call in rounds[-1].get('env_calls', []):
        feedback = call.get('feedback') or {}
        request = call['request']
        malformed_not_started = (
            not request.get('request_id')
            and feedback.get('request_id') == 'invalid'
            and feedback.get('status') == 'error'
            and feedback.get('execution', {}).get('state') == 'not_started'
            and feedback.get('error', {}).get('type') == 'ValueError'
        )
        if malformed_not_started:
            # The public gateway's malformed-envelope rejection has no valid
            # RPC identity. Keep it verbatim and never add it to replay work.
            unresolved = is_uncertain(feedback)
        else:
            unresolved = is_uncertain(feedback, request=request)
        if unresolved and (readonly is None or not readonly(call['request'])):
            raise ValueError('Unresolved environment execution must be recovered first')
    checkpoint = source / 'method/state/baseline-continuation.json'
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding='utf-8'))
        if (
            state.get('schema') != 'baseline.continuation.v1'
            or state['round_number'] != rounds[-1]['round']
        ):
            raise ValueError('Checkpoint/report round mismatch')
        if state.get('charged_tokens') != report['token_usage']['charged_tokens']:
            raise ValueError('Checkpoint accounting is stale relative to final report')
    else:
        if not models:
            raise ValueError('No provider input from which to restore SmartRefactorer memory')
        request = models[-1]['request']
        context = None
        for message in request.get('input', []):
            if not isinstance(message, dict) or message.get('role') != 'user':
                continue
            contents = message.get('content', [])
            if isinstance(contents, str):
                contents = [{'text': contents}]
            for part in contents:
                if not isinstance(part, dict) or not isinstance(part.get('text'), str):
                    continue
                try:
                    value = decode(part['text'])
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(value, dict) and 'history' in value:
                    context = value
        if context is None:
            raise ValueError('Last provider context is not reconstructable')
        history = copy.deepcopy(context['history'])
        total = context.get('history_access', {}).get('original_decisions', len(history))
        memory = context.get('working_memory', '')
        call_map = {c['call_id']: c for c in calls}
        steps = sorted(
            (step for record in rounds for step in record.get('steps', [])),
            key=lambda s: s['decision_index'],
        )
        for step in steps:
            if step['decision_index'] <= total:
                continue
            if step['decision_index'] != total + 1:
                raise ValueError('Decision gap cannot be reconstructed exactly')
            decision = step['decision']
            item = {
                'decision': {'decision': decision['decision'], 'report': decision['report']},
                'calls': [],
            }
            for cid in step.get('calls', []):
                call = call_map[cid]
                # The newest paid decision's results have not appeared in another
                # provider input. Selected views must be retained explicitly.
                feedback = call.get('response_view')
                if feedback is None:
                    options = call['request'].get('response') or {}
                    if options and options.get('detail') != 'full':
                        raise ValueError(
                            'Unrecorded selected feedback view requires an explicit checkpoint'
                        )
                    feedback = call['feedback']
                item['calls'].append(
                    {
                        'request': copy.deepcopy(call['request']),
                        'feedback': copy.deepcopy(feedback),
                    }
                )
            for key in ('feedback', 'method_feedback', 'unexecuted_requests'):
                if key in step:
                    item[key] = copy.deepcopy(step[key])
            history.append(item)
            total += 1
            if decision['decision'] == 'remember':
                memory = decision['report']
        state = {
            'schema': 'baseline.continuation.v1',
            'round_number': rounds[-1]['round'],
            'history': history[-6:],
            'history_total': total,
            'working_memory': memory,
        }
    last = rounds[-1]
    pending = []
    if last.get('steps'):
        step = last['steps'][-1]
        if step['decision']['decision'] == 'request' and not step.get('stop_reason'):
            planned = json.loads(step['decision']['request_json'])
            planned = [planned] if isinstance(planned, dict) else planned
            completed = len(step.get('calls', []))
            if completed < len(planned):
                pending = copy.deepcopy(planned[completed:])
    pending = []  # Method programs resume their durable cursor, never a runner batch prefix.
    unstarted = None
    if expected:
        method_pending = json.loads(development.read_text(encoding='utf-8')).get('pending')
        if (
            method_pending
            and method_pending.get('receipt') is None
            and not method_pending.get('receipt_conflict')
            and method_pending['request'].get('request_id') not in env_request_ids
        ):
            unstarted = copy.deepcopy(method_pending['request'])
    state.update(
        verified_unstarted_method_request=unstarted,
        source_run_id=report['run_id'],
        source_dir=str(source),
        source_report_sha256=hashlib.sha256(raw).hexdigest(),
        report=report,
        rounds=rounds,
        models=models,
        env_calls=calls,
        pending_requests=pending,
        selected_task=copy.deepcopy(last.get('selected_task') or last.get('final_task')),
    )
    return state


def restore_recorder(recorder, state):
    recorder.verified_unstarted_method_request = copy.deepcopy(
        state.get('verified_unstarted_method_request')
    )
    recorder.models = copy.deepcopy(state['models'])
    recorder.env_calls = copy.deepcopy(state['env_calls'])
    recorder.charged = state['report']['token_usage']['charged_tokens']
    recorder.known_tokens = state['report']['token_usage'].get('total_tokens', recorder.charged)
    recorder.unknown_usage = any(
        call.get('usage_status') == 'unknown_reserved_upper_bound' for call in recorder.models
    )
    recorder.rounds = copy.deepcopy(state['rounds'][:-1])
    recorder.round = copy.deepcopy(state['rounds'][-1])
    recorder.round['continuation_timing'] = {
        'carried_wall_seconds': recorder.round.get('wall_seconds', 0),
        'carried_model_wall_seconds': recorder.round.get(
            'model_wall_seconds',
            sum(c.get('wall_seconds', 0) for c in recorder.round.get('model_calls', [])),
        ),
        'carried_env_wall_seconds': recorder.round.get(
            'env_wall_seconds',
            sum(c.get('wall_seconds', 0) for c in recorder.round.get('env_calls', [])),
        ),
        'wall_seconds_semantics': 'sum_of_active_segments_excluding_operator_downtime',
    }
    for key in (
        'finished_at',
        'wall_seconds',
        'end_reason',
        'audit',
        'task_workspace_release',
        'task_workspace_release_error',
    ):
        recorder.round.pop(key, None)
    recorder.round_started = (
        time.perf_counter() - recorder.round['continuation_timing']['carried_wall_seconds']
    )
    recorder.active_task = copy.deepcopy(state['selected_task'])
    for call in recorder.env_calls:
        if (
            call['request']['op'] == 'task.start'
            and call['feedback'].get('status') == 'completed'
        ):
            instance = call['feedback']['result']
            recorder.instances[instance['id']] = copy.deepcopy(instance)


def copy_context_sources(source, destination):
    source = Path(source) / 'method/artifacts/context'
    destination = Path(destination) / 'method/artifacts/context'
    if not source.is_dir():
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in source.glob('*.json'):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            raise ValueError('Source context hash mismatch')
        target = destination / path.name
        if target.exists() and target.read_bytes() != raw:
            raise ValueError('Destination context collision')
        if not target.exists():
            shutil.copyfile(path, target)
        count += 1
    return count


class ContinuationService:
    """Inject this method's verified continuation contract into the generic host."""

    load_continuation = staticmethod(load_continuation)
    restore_recorder = staticmethod(restore_recorder)
    copy_context_sources = staticmethod(copy_context_sources)

    @staticmethod
    def save_runner_checkpoint(folder, context, recorder, memory):
        # Retain the established on-disk schema/name for historical resume.
        from audit.recorder import save_json

        save_json(
            Path(folder) / 'method/state/baseline-continuation.json',
            {
                'schema': 'baseline.continuation.v1',
                'round_number': recorder.round['round'],
                'history': copy.deepcopy(context['history'][-6:]),
                'history_total': context.get('history_offset', 0) + len(context['history']),
                'working_memory': memory,
                'charged_tokens': recorder.charged,
                'event_sequence': recorder.sequence,
            },
        )
