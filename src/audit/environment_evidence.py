"""Bound canonical environment receipts carried across control exceptions.

Only trusted environment code attaches receipts after journaling them. This
module neither guesses execution outcomes nor depends on a task domain.
"""
import copy
import hashlib
import json


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


class _EnvironmentFeedbackEvidence:
    __slots__=('request_hash','feedback_hash','request','feedback')
    def __init__(self,request,feedback):
        self.request=copy.deepcopy(request);self.feedback=copy.deepcopy(feedback)
        self.request_hash=_digest(self.request);self.feedback_hash=_digest(self.feedback)


def _valid(request,feedback):
    return (isinstance(request,dict) and isinstance(request.get('request_id'),str)
        and isinstance(feedback,dict) and feedback.get('request_id')==request['request_id']
        and all(k in feedback for k in ('protocol_version','session_id','status','result','error','execution'))
        and feedback['status'] in ('completed','error','running','pending_or_unknown')
        and isinstance(feedback['execution'],dict)
        and feedback['execution'].get('state') in ('completed','not_started','running','unknown'))


def attach_environment_feedback(exc,request,feedback):
    """Attach a defensive copy of an already journaled full receipt."""
    if not isinstance(exc,BaseException) or not _valid(request,feedback):
        raise ValueError('Canonical feedback must be bound to its exact request')
    exc._environment_feedback_evidence=_EnvironmentFeedbackEvidence(request,feedback)


def get_environment_feedback(exc,request):
    """Return verified bound evidence, never an arbitrary exception dictionary."""
    evidence=getattr(exc,'_environment_feedback_evidence',None)
    if type(evidence) is not _EnvironmentFeedbackEvidence:return None
    try:
        if (_digest(request)!=evidence.request_hash or _digest(evidence.request)!=evidence.request_hash
            or _digest(evidence.feedback)!=evidence.feedback_hash or not _valid(request,evidence.feedback)):
            return None
    except (TypeError,ValueError,AttributeError):return None
    return copy.deepcopy(evidence.feedback)
