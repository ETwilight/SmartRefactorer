"""Explicit presentation views; canonical feedback is always retained separately."""

import copy


def brief(value, depth=0):
    if isinstance(value, str):
        return (
            value
            if len(value) <= 1200
            else value[:1200] + ' [truncated; retrieve full feedback]'
        )
    if isinstance(value, list):
        if depth >= 3:
            return {'items': len(value), 'detail': 'Retrieve the original result by request ID'}
        return [brief(x, depth + 1) for x in value[:8]] + (
            [{'remaining_items': len(value) - 8}] if len(value) > 8 else []
        )
    if isinstance(value, dict):
        if depth >= 4:
            return {'fields': list(value), 'detail': 'Retrieve selected fields by request ID'}
        return {k: brief(v, depth + 1) for k, v in value.items()}
    return value


def selected(value, paths):
    result = {}
    for path in paths:
        node = value
        for key in path.split('.'):
            node = node[int(key)] if isinstance(node, list) else node[key]
        result[path] = copy.deepcopy(node)
    return result


def present(feedback, options=None):
    if __package__:
        from .region_views import is_region, present_region
    else:
        from region_views import is_region, present_region
    if options and 'sections' in options:
        if not isinstance(options['sections'], list):
            raise ValueError('Region sections must be an array')
        if not is_region(feedback):
            raise ValueError('Sections apply only to completed observe.region results')
        if options.get('fields') or options.get('detail') == 'reference':
            raise ValueError(
                'Region sections cannot be combined with fields or reference detail'
            )
        return present_region(feedback, options['sections'])
    if (
        is_region(feedback)
        and (not options or options.get('detail') == 'summary')
        and not (options or {}).get('fields')
    ):
        return present_region(feedback)
    options = options or {}
    detail = options.get('detail', 'full')
    if detail == 'full' and not options.get('fields'):
        return copy.deepcopy(feedback)
    result = copy.deepcopy(feedback)
    original = feedback.get('result')
    if (
        isinstance(original, dict)
        and original.get('infrastructure')
        and 'infrastructure' not in result
    ):
        result['infrastructure'] = copy.deepcopy(original['infrastructure'])
    if options.get('fields'):
        result['result'] = selected(feedback.get('result'), options['fields'])
    elif detail == 'reference':
        result['result'] = None
    else:
        value = feedback.get('result')
        if (
            isinstance(value, dict)
            and value.get('method') == 'command.run'
            and isinstance(value.get('result'), dict)
        ):
            body = value['result']
            receipt = {
                k: copy.deepcopy(body[k])
                for k in (
                    'tick',
                    'effect',
                    'actual_changes',
                    'postcondition_verified',
                    'callbacks',
                    'message_keys',
                )
                if k in body
            }
            # Native command output may contain the answer to a read-only query.
            text = body.get('command', '').lstrip('/')
            read = (
                text.startswith(('data get ', 'forceload query', 'tick query', 'list'))
                or ' run data get ' in text
            )
            if read or not any(c.get('success') for c in body.get('callbacks', [])):
                receipt['messages'] = brief(body.get('messages', []))
            result['result'] = {
                'method': 'command.run',
                'sample_wall_ms': value.get('sample_wall_ms'),
                'result': receipt,
            }
        elif isinstance(value, dict) and 'verdict' in value and 'checks' in value:
            result['result'] = {
                k: copy.deepcopy(value[k])
                for k in (
                    'evaluation_id',
                    'task_instance_id',
                    'task_id',
                    'verdict',
                    'goal_passed',
                    'check_pass_fraction',
                    'evaluation_mode',
                )
                if k in value
            }
            result['result']['checks'] = [
                copy.deepcopy(c)
                for c in value['checks']
                if c['status'] in ('fail', 'inconclusive')
            ]
            result['result']['check_counts'] = {
                s: sum(c['status'] == s for c in value['checks'])
                for s in ('pass', 'fail', 'inconclusive', 'not_applicable')
            }
            result['result']['production'] = copy.deepcopy(
                value.get('metrics', {}).get('production', {})
            )
            result['result']['cleanup_errors'] = copy.deepcopy(value.get('cleanup_errors', []))
            # The envelope carries the typed incident in every view; avoid
            # duplicating it in the report summary.
        elif (
            isinstance(value, dict)
            and isinstance(value.get('items'), list)
            and all(
                isinstance(v, dict) and 'request_id' in v and 'status' in v
                for v in value['items']
            )
        ):
            # feedback.list is already explicitly paged; keep incident metadata.
            result['result'] = copy.deepcopy(value)
        else:
            result['result'] = brief(value)
    result['feedback_ref'] = feedback['request_id']
    result['response_view'] = options
    return result
