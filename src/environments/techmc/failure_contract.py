"""Normalize infrastructure failures, execution certainty, and evaluation reports.

Response status describes the protocol result. execution records whether
dispatch started or completed, while infrastructure records measurement,
cleanup, and persistence failures that affect the evaluation verdict.
"""

import copy
import json
from pathlib import Path

if __package__:
    from .contracts import VERSION, validate
    from .actions.catalog import request_effect
    from .runtime_support import atomic_json
    from .task_catalog import stable_hash
else:
    from contracts import VERSION, validate
    from actions.catalog import request_effect
    from runtime_support import atomic_json
    from task_catalog import stable_hash


def fault(exc):
    """Extract the original exception type and message, including notes, for a bounded error
    record.
    """
    # Unwrap the pre-dispatch exception to retain the original validation error.
    # The caller carries dispatch certainty separately in execution metadata.
    # exc may change from an OperationNotStarted wrapper to its underlying exception.

    exc = getattr(exc, 'original_exception', exc)
    message = str(exc)
    notes = getattr(exc, '__notes__', None)
    if notes:
        message += '; ' + '; '.join(str(note) for note in notes)
    return {'type': type(exc).__name__, 'message': message[:4000]}


def request_hash(request):
    """Hash the request with response-presentation options omitted."""

    return stable_hash({k: v for k, v in request.items() if k != 'response'})


def is_report(value):
    """Recognize an evaluation-report dictionary by its required identity and result fields."""

    return isinstance(value, dict) and {
        'evaluation_id',
        'task_instance_id',
        'task_id',
        'verdict',
        'checks',
        'metrics',
    } <= set(value)


def report(value):
    """Normalize infrastructure and cleanup outcomes, then validate the copied evaluation
    report.
    """

    value = copy.deepcopy(value)
    interrupted = value.get('metrics', {}).get('infrastructure_failure')
    if interrupted:
        info = value.setdefault(
            'infrastructure', {'status': 'measurement_interrupted', 'recovered': False}
        )
        info.setdefault('fault', interrupted)
        info['measurement_valid'] = False
        info.setdefault('stage', 'measurement')
    if value.get('cleanup_errors') and 'infrastructure' not in value:
        value['infrastructure'] = {
            'status': 'measurement_interrupted',
            'stage': 'cleanup',
            'recovered': False,
            'fault': {
                'type': 'CleanupError',
                'message': ' | '.join(value['cleanup_errors'])[:4000],
            },
        }
        if value['verdict'] != 'fail':
            value['infrastructure']['measurement_valid'] = False
    if value.get('infrastructure', {}).get('measurement_valid') is False:
        value['verdict'] = 'inconclusive'
    if value['verdict'] == 'inconclusive':
        value['goal_passed'] = None
    validate('evaluation-report', value)
    return value


def execution(request, response, started=True, known_state=None):
    """Derive execution state and retry instructions from response status and dispatch
    certainty.
    """

    status = response['status']
    op = request.get('op')
    if (known_state == 'not_started' or not started) and op in (
        'actions.get',
        'schemas.get',
        'environment.document',
        'tasks.get',
        'feedback.get',
        'registry.get',
    ):
        return {
            'state': 'not_started',
            'retry': 'none',
            'next_steps': [
                'This lookup did not execute. Correct its action or operation name using the published catalogue/request schema; do not resend the unchanged invalid lookup.'
            ],
        }
    if known_state == 'not_started' or not started:
        return {
            'state': 'not_started',
            'retry': 'observe_before_new_request',
            'next_steps': [
                'Correct the request or resolve the reported blocker before issuing a new request.'
            ],
        }
    if known_state == 'completed' and status != 'completed':
        return {
            'state': 'completed',
            'retry': 'none',
            'next_steps': [
                'This execution attempt completed. Inspect the retained result and reported error before issuing a new request.'
            ],
        }
    if status == 'completed':
        retry = (
            'new_evaluation'
            if is_report(response.get('result'))
            and response['result']['verdict'] == 'inconclusive'
            else 'none'
        )
        if retry == 'new_evaluation' and response['result']['metrics'].get('review_blocked'):
            return {
                'state': 'completed',
                'retry': retry,
                'next_steps': [
                    'Measurement did not start. Resolve the reported model credentials or host budget blocker, then issue a new evaluation request ID; no world recovery is required for this blocker.'
                ],
            }
        return {
            'state': 'completed',
            'retry': retry,
            'next_steps': (
                [
                    'The evaluation attempt completed, but the machine outcome is unknown. Inspect infrastructure and world state before a new evaluation.'
                ]
                if retry == 'new_evaluation'
                else []
            ),
        }
    if status == 'running':
        return {
            'state': 'running',
            'retry': 'none',
            'next_steps': ['Query action.status; do not start another copy.'],
        }
    read_only = request_effect(request) == 'read_only'
    return {
        'state': 'unknown',
        'retry': 'read_only_new_request' if read_only else 'observe_before_new_request',
        'next_steps': (
            ['Retry the read-only query when connectivity is restored.']
            if read_only
            else [
                'The operation may have executed. Preserve this request ID and payload. Inspect feedback and actual world state; do not blindly replay or replace the mutation.'
            ]
        ),
    }


def response(value, request=None):
    """Copy a response, normalize any evaluation report, fill execution metadata, and
    validate it.
    """

    value = copy.deepcopy(value)
    if is_report(value.get('result')):
        value['result'] = report(value['result'])
        if value['result'].get('infrastructure'):
            value['infrastructure'] = copy.deepcopy(value['result']['infrastructure'])
    if request is not None and 'execution' not in value:
        value['execution'] = execution(request, value)
    validate('response', value)
    return value


def view(value, options=None):
    """Select a response view or return a selection error referencing the retained original
    feedback.
    """

    if __package__:
        from .response_views import present
    else:
        from response_views import present
    try:
        result = present(value, options)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        result = {
            **copy.deepcopy(value),
            'status': 'error',
            'result': None,
            'feedback_ref': value['request_id'],
            'response_view': options or {},
            'error': {
                'type': 'ResponseSelectionError',
                'message': 'Original operation status='
                + value['status']
                + '. Only selection failed: '
                + str(exc)
                + '. Retrieve feedback; do not repeat the operation.',
            },
        }
    validate('response', result)
    return result


def interrupted_report(instance, submission, evaluation_id, directory, exc):
    """Construct a validated inconclusive evaluation report for an interrupted measurement."""

    cause = fault(exc)
    return report(
        {
            'protocol_version': VERSION,
            'evaluation_id': evaluation_id,
            'task_instance_id': instance['id'],
            'task_id': instance['task']['id'],
            'task_hash': instance['task']['task_hash'],
            'submission_hash': stable_hash(submission),
            'verdict': 'inconclusive',
            'goal_passed': None,
            'check_pass_fraction': 0,
            'checks': [
                {
                    'id': 'measurement_unavailable',
                    'status': 'inconclusive',
                    'message': cause['message'],
                    'evidence': [],
                }
            ],
            'metrics': {'infrastructure_failure': cause},
            'ai_review': None,
            'evidence_path': str(directory),
            'cleanup_errors': [],
        }
    )


def persist_report(root, value):
    """Save normalized evaluation and evidence files, retaining the first replaced report
    separately.
    """
    value = report(value)
    directory = Path(root) / 'runs' / value['evaluation_id']
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'evaluation.json'
    if path.exists():
        original = json.loads(path.read_text(encoding='utf-8'))
        before = directory / 'pre-finalization-report.json'
        if original != value and not before.exists():
            atomic_json(before, original)
    evidence_path = directory / 'evidence.json'
    evidence = (
        json.loads(evidence_path.read_text(encoding='utf-8'))
        if evidence_path.exists()
        else {'observations': []}
    )
    # evidence.result and value now refer to the same report dictionary.
    evidence['result'] = value
    atomic_json(evidence_path, evidence)
    atomic_json(path, value)
    return value


def write_measurement(directory, evidence, value):
    """Update the report in place, write its evidence, and mark measurement invalid if
    persistence fails.
    """
    normalized = report(value)
    # Keep this dict's identity while replacing its contents with the normalized report.
    value.clear()
    value.update(normalized)
    # evidence.result and value now refer to the same report dictionary.
    evidence['result'] = value
    try:
        atomic_json(directory / 'evidence.json', evidence)
        atomic_json(directory / 'evaluation.json', value)
    except Exception as exc:
        info = value.setdefault(
            'infrastructure',
            {'status': 'persistence_failed', 'stage': 'persistence', 'recovered': False},
        )
        info['persistence_error'] = fault(exc)
        info['measurement_valid'] = False
        normalized = report(value)
        # Keep this dict's identity while replacing its contents with the normalized report.
        value.clear()
        value.update(normalized)
        for path, body in (
            (directory / 'evidence.json', evidence),
            (directory / 'evaluation.json', value),
        ):
            try:
                atomic_json(path, body)
            except Exception:
                pass
    return value
