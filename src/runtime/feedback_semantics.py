"""Read execution state, retry information, and status from public feedback.

_check validates the raw feedback mapping and its request identity. These
helpers then select execution fields or the status fallback used by older
feedback records. Read-only classification comes from tool effect metadata.
"""

from collections.abc import Mapping


def _check(feedback, request=None):
    """Validate a feedback mapping and return its execution metadata."""

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
    """Return the published execution state, or None when it is absent."""
    return _check(feedback, request).get('state')


def is_uncertain(feedback, *, request=None):
    """Return whether execution or feedback status reports unknown or running work."""

    state = execution_state(feedback, request=request)
    if state is not None:
        return state in ('unknown', 'running')
    # Use the status field when the record has no execution metadata.
    return feedback.get('status') in ('pending_or_unknown', 'running')


def is_terminal(feedback, *, request=None):
    """Return whether execution metadata names completed or not_started."""

    return execution_state(feedback, request=request) in ('completed', 'not_started')


def is_running(feedback, *, request=None):
    """Return whether execution metadata or feedback status reports running work."""
    state = execution_state(feedback, request=request)
    return state == 'running' if state is not None else feedback.get('status') == 'running'


def retry_directive(feedback, *, request=None):
    """Return the retry directive stored in the execution metadata."""
    return _check(feedback, request).get('retry')


def operation_succeeded(feedback, *, request=None):
    """Return whether the request feedback status is completed."""
    _check(feedback, request)
    return feedback.get('status') == 'completed'


def is_read_only(metadata):
    """Return whether tool metadata declares effect=read_only."""

    return isinstance(metadata, Mapping) and metadata.get('effect') == 'read_only'
