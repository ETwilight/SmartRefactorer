"""Track feedback omitted from model inputs rejected for exceeding the context limit."""

import copy
import hashlib
import json
from runtime.context.codec import decode
from runtime.context.store import canonical

GUIDE = (
    ' Entries with model_delivery_error describe a provider-rejected input, not an environment failure. '
    'Their original feedback was not delivered to the model and remains at exact_feedback. '
    'feedback_index lists available values and exact subtree references. Read narrower fields or array '
    'indices with read_context; an availability index is not its unseen contents. Previously accepted '
    'tool results remain fully supplied.'
)


def _fingerprint(value):
    """Hash the canonical JSON bytes of a retained feedback value."""

    return hashlib.sha256(canonical(value)).hexdigest()


def _input_history(call, field):
    """Extract a named history list from a recorded user message, or return None."""

    for message in call.get('request', {}).get('input', []):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue
        content = message.get('content', [])
        if isinstance(content, str):
            # content changes from a plain string to a list of message-part dictionaries.
            content = [{'text': content}]
        for part in content:
            if not isinstance(part, dict):
                continue
            try:
                # value: decoded JSON; only an object containing the requested history list is
                # used.
                value = decode(part['text'])
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(value, dict) and isinstance(value.get(field), list):
                return value[field]
    return None


def _context_refusal(call):
    """Recognize an HTTP 400 context_length_exceeded response from the provider."""

    error = call.get('error') or {}
    if not isinstance(error, dict):
        return False
    try:
        body = json.loads(error.get('response_body', ''))
    except (ValueError, TypeError):
        return False
    detail = body.get('error') if isinstance(body, dict) else None
    response = call.get('response')
    returned_error = response.get('error') if isinstance(response, dict) else None
    error_body_only = (
        isinstance(response, dict)
        and set(response) == {'error'}
        and isinstance(returned_error, dict)
        and returned_error.get('code') == 'context_length_exceeded'
    )
    return (
        error.get('http_status') == 400
        and (not response or error_body_only)
        and isinstance(detail, dict)
        and detail.get('code') == 'context_length_exceeded'
    )


def _feedback_index(feedback, evidence):
    """Present shallow result summaries with references for reading their contents."""

    for field in ('items', 'results'):
        if isinstance(feedback.get(field), list):
            return {
                field: [
                    {
                        **{key: item[key] for key in ('id', 'path', 'status') if key in item},
                        'value': evidence.presentation(item.get('result', item), depth=1),
                    }
                    for item in feedback[field]
                ]
            }
    return evidence.presentation(feedback, depth=1)


def history_views(session, evidence):
    """Build model-facing history using recorded delivery failures.

    Entries with rejected feedback carry its availability index and exact stored
    reference. The session retains the original feedback; its hash is checked
    before constructing each replacement entry.
    """
    history = session['history']
    # failures: dict[str, dict], keyed by stringified history positions.
    failures = session.setdefault('delivery_failures', {})
    result = []
    for index, entry in enumerate(history):
        failure = failures.get(str(index))
        if failure is None:
            result.append(entry)
            continue
        if _fingerprint(entry['feedback']) != failure['feedback_sha256']:
            raise ValueError(
                'Rejected-delivery record differs from its retained original feedback'
            )
        result.append(
            {
                **{key: value for key, value in entry.items() if key != 'feedback'},
                'model_delivery_error': {
                    key: value for key, value in failure.items() if key != 'feedback_index'
                },
                'feedback_index': failure['feedback_index'],
            }
        )
    return result


def recover_rejected_delivery(session, calls, scope, evidence, field='tool_history'):
    """Record newly undelivered feedback after a matching context-limit refusal.

    Compare the rejected input with the current history view, then use earlier
    accepted inputs to find the delivered prefix. Store references and indexes
    for the remaining results. Return whether any failure records were added.
    """
    relevant = [call for call in calls if call.get('scope') == scope]
    if not relevant or not _context_refusal(relevant[-1]):
        return False
    refused = relevant[-1]
    views = history_views(session, evidence)
    if _input_history(refused, field) != views:
        return False
    # accepted: int, the length of the history prefix present in accepted model inputs.
    accepted = session.get('accepted_history_length', 0)
    for call in relevant[:-1]:
        if not call.get('response') or _context_refusal(call):
            continue
        prior = _input_history(call, field)
        if prior is not None and len(prior) <= len(views) and prior == views[: len(prior)]:
            accepted = max(accepted, len(prior))
    session['accepted_history_length'] = accepted
    # failures: dict[str, dict], keyed by stringified history positions.
    failures = session.setdefault('delivery_failures', {})
    changed = False
    for index in range(accepted, len(session['history'])):
        if str(index) in failures:
            continue
        feedback = session['history'][index]['feedback']
        failures[str(index)] = {
            'type': 'ModelContextDeliveryRejected',
            'call_id': refused.get('call_id'),
            'message': 'The provider rejected the input containing this new result. The local read completed; request a narrower exact subtree.',
            'feedback_sha256': _fingerprint(feedback),
            'exact_feedback': {'id': evidence.contexts.put(feedback), 'path': ''},
            'provider_error': copy.deepcopy(refused.get('error')),
            'feedback_index': _feedback_index(feedback, evidence),
        }
        changed = True
    return changed
