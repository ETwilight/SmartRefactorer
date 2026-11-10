"""Read canonical feedback without confusing execution with business success.

The caller obtains the full, payload-bound receipt from its environment port.
These functions neither establish that provenance nor rewrite the receipt.
Read-only authority is provider metadata, never a method-name heuristic.
"""

from collections.abc import Mapping


def _check(feedback, request=None):
    if not isinstance(feedback, Mapping):
        raise ValueError('Canonical feedback must be an object')
    if feedback.get('feedback_ref') or feedback.get('response_view'):
        raise ValueError('Canonical feedback must not be a presentation or reference')
    if request is not None and (
        not request.get('request_id') or feedback.get('request_id') != request['request_id']
    ):
        raise ValueError('Canonical feedback does not match the request identity')
    execution = feedback.get('execution', {})
    if not isinstance(execution, Mapping):
        raise ValueError('Canonical execution must be an object')
    state = execution.get('state')
    if state is not None and state not in ('not_started', 'completed', 'running', 'unknown'):
        raise ValueError('Unknown canonical execution state')
    status = feedback.get('status')
    if (
        status in ('running', 'pending_or_unknown') and state in ('completed', 'not_started')
    ) or (status == 'completed' and state in ('unknown', 'running')):
        raise ValueError('Contradictory canonical status and execution state')
    return execution


def execution_state(feedback, *, request=None):
    """Return only the explicitly published execution state, otherwise None."""
    return _check(feedback, request).get('state')


def is_uncertain(feedback, *, request=None):
    state = execution_state(feedback, request=request)
    if state is not None:
        return state in ('unknown', 'running')
    # Compatibility with older ports which did not publish execution metadata.
    return feedback.get('status') in ('pending_or_unknown', 'running')


def is_terminal(feedback, *, request=None):
    return execution_state(feedback, request=request) in ('completed', 'not_started')


def is_running(feedback, *, request=None):
    """Known asynchronous work uses status polling, not unknown recovery."""
    state = execution_state(feedback, request=request)
    return state == 'running' if state is not None else feedback.get('status') == 'running'


def retry_directive(feedback, *, request=None):
    """Read the producer's retry decision; do not invent permission to resend."""
    return _check(feedback, request).get('retry')


def operation_succeeded(feedback, *, request=None):
    """Operation success is not independent evaluation or task acceptance."""
    _check(feedback, request)
    return feedback.get('status') == 'completed'


def is_read_only(metadata):
    return isinstance(metadata, Mapping) and metadata.get('effect') == 'read_only'
