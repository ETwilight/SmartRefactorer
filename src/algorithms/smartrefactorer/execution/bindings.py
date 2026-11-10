"""Explicit JSON argument templates; no environment rules or implicit parameters."""

import copy, json, re


class BindingError(ValueError):
    pass


def pointer(path):
    return (
        isinstance(path, str)
        and (not path or path.startswith('/'))
        and not re.search(r'~(?![01])', path)
    )


def read_pointer(target, path):
    try:
        for token in path.split('/')[1:] if path else []:
            token = token.replace('~1', '/').replace('~0', '~')
            if isinstance(target, list):
                if (
                    not token
                    or any(c not in '0123456789' for c in token)
                    or len(token) > 1
                    and token.startswith('0')
                ):
                    raise ValueError('Invalid array index')
                target = target[int(token)]
            else:
                target = target[token]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise BindingError('Missing or invalid binding pointer ' + path) from exc
    return copy.deepcopy(target)


def validate_template(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise BindingError('JSON object keys must be strings')
        if set(value) == {'$literal'}:
            try:
                if (
                    json.loads(json.dumps(value['$literal'], allow_nan=False))
                    != value['$literal']
                ):
                    raise ValueError('Literal changes during JSON serialization')
            except (ValueError, TypeError) as exc:
                raise BindingError('Literal is not a JSON value') from exc
            return
        if set(value) == {'$public'}:
            path = value['$public']
            if not pointer(path):
                raise BindingError('$public requires a JSON Pointer')
            return
        if set(value) == {'$evidence'}:
            ref = value['$evidence']
            if (
                not isinstance(ref, dict)
                or set(ref) != {'id', 'path'}
                or not isinstance(ref['id'], str)
                or len(ref['id']) != 64
                or any(c not in '0123456789abcdef' for c in ref['id'])
            ):
                raise BindingError('$evidence requires an exact evidence id and path')
            if not pointer(ref['path']):
                raise BindingError('Evidence path must be a JSON Pointer')
            return
        for child in value.values():
            validate_template(child)
    elif isinstance(value, list):
        for child in value:
            validate_template(child)
    elif isinstance(value, str) and value.startswith('runtime.'):
        raise BindingError(
            'Descriptive runtime.* placeholders are not values; use $public, $evidence, or an explicit $literal'
        )
    else:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise BindingError('Argument is not a JSON value') from exc


def resolve_arguments(template, public, evidence=None):
    validate_template(template)

    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {'$literal'}:
                return copy.deepcopy(value['$literal'])
            if set(value) == {'$public'}:
                return read_pointer(public, value['$public'])
            if set(value) == {'$evidence'}:
                if evidence is None:
                    raise BindingError('No evidence reader supplied')
                ref = value['$evidence']
                try:
                    return read_pointer(evidence(ref['id'], ''), ref['path'])
                except (KeyError, IndexError, ValueError, TypeError) as exc:
                    raise BindingError(
                        'Invalid evidence binding ' + ref['id'] + ref['path']
                    ) from exc
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return copy.deepcopy(value)

    result = resolve(template)
    try:
        if json.loads(json.dumps(result, allow_nan=False)) != result:
            raise ValueError('Binding changes during JSON serialization')
    except (ValueError, TypeError) as exc:
        raise BindingError('Resolved arguments must remain exact JSON values') from exc
    return result
