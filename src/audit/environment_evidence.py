"""Bind journaled environment receipts to exceptions using copied payloads and hashes."""

import copy
import hashlib
import json


def _digest(value):
    """Hash canonical request or feedback JSON."""

    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
        ).encode('utf-8')
    ).hexdigest()


class _EnvironmentFeedbackEvidence:
    """Store copied request/feedback payloads and their content digests.

    The exception attachment keeps request_hash, feedback_hash, request, and
    feedback. Receipt lookup checks the supplied request and both retained copies.

    Example:
        evidence = _EnvironmentFeedbackEvidence(request, feedback)

    This captures the payloads used by attach_environment_feedback.
    """

    __slots__ = ('request_hash', 'feedback_hash', 'request', 'feedback')

    def __init__(self, request, feedback):
        """Copy the paired payloads and calculate their canonical hashes."""

        self.request = copy.deepcopy(request)
        self.feedback = copy.deepcopy(feedback)
        self.request_hash = _digest(self.request)
        self.feedback_hash = _digest(self.feedback)


def _valid(request, feedback):
    """Check request/feedback identity and the required protocol execution fields."""

    return (
        isinstance(request, dict)
        and isinstance(request.get('request_id'), str)
        and isinstance(feedback, dict)
        and feedback.get('request_id') == request['request_id']
        and all(
            k in feedback
            for k in (
                'protocol_version',
                'session_id',
                'status',
                'result',
                'error',
                'execution',
            )
        )
        and feedback['status'] in ('completed', 'error', 'running', 'pending_or_unknown')
        and isinstance(feedback['execution'], dict)
        and feedback['execution'].get('state')
        in ('completed', 'not_started', 'running', 'unknown')
    )


def attach_environment_feedback(exc, request, feedback):
    """Attach copies of an already journaled full receipt to its control exception."""
    if not isinstance(exc, BaseException) or not _valid(request, feedback):
        raise ValueError('Canonical feedback must be bound to its exact request')
    exc._environment_feedback_evidence = _EnvironmentFeedbackEvidence(request, feedback)


def get_environment_feedback(exc, request):
    """Copy the attached receipt when its request and stored content hashes still match."""
    evidence = getattr(exc, '_environment_feedback_evidence', None)
    if type(evidence) is not _EnvironmentFeedbackEvidence:
        return None
    try:
        if (
            _digest(request) != evidence.request_hash
            or _digest(evidence.request) != evidence.request_hash
            or _digest(evidence.feedback) != evidence.feedback_hash
            or not _valid(request, evidence.feedback)
        ):
            return None
    except (TypeError, ValueError, AttributeError):
        return None
    return copy.deepcopy(evidence.feedback)
