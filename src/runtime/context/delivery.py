"""将原始反馈组织为模型上下文中的精确引用与分页结果。

存储内容和展示内容分别维护；读取请求携带原始记录身份与范围。
"""

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
    return hashlib.sha256(canonical(value)).hexdigest()


def _input_history(call, field):
    for message in call.get('request', {}).get('input', []):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue
        content = message.get('content', [])
        if isinstance(content, str):
            content = [{'text': content}]
        for part in content:
            if not isinstance(part, dict):
                continue
            try:
                value = decode(part['text'])
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(value, dict) and isinstance(value.get(field), list):
                return value[field]
    return None


def _context_refusal(call):
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
    """Keep raw history and every received result exact; mark proven failed delivery."""
    history = session['history']
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
    """Use actual accepted/refused inputs to identify only undelivered results.

    If every result was already received, or this exact failure was already
    handled, return false: another identical generation must not be attempted.
    """
    relevant = [call for call in calls if call.get('scope') == scope]
    if not relevant or not _context_refusal(relevant[-1]):
        return False
    refused = relevant[-1]
    views = history_views(session, evidence)
    if _input_history(refused, field) != views:
        return False
    accepted = session.get('accepted_history_length', 0)
    for call in relevant[:-1]:
        if not call.get('response') or _context_refusal(call):
            continue
        prior = _input_history(call, field)
        if prior is not None and len(prior) <= len(views) and prior == views[: len(prior)]:
            accepted = max(accepted, len(prior))
    session['accepted_history_length'] = accepted
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
