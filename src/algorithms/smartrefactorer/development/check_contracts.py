"""Check contracts shared by proposal admission and deterministic validation.

Complete invocation fixtures and ordinary Python assertions are executable.
Narrative intent is corrected at Propose before further generation. Python
checks receive candidate modules and fixture requests, never a live environment;
they do not replace valid-old-input preservation evidence.
"""

from jsonschema import Draft202012Validator
from ..execution import python_backend
from ..library import digest

REQUESTS = {
    'type': 'array',
    'items': {
        'type': 'object',
        'required': ['request', 'feedback'],
        'properties': {'request': {'type': 'object'}, 'feedback': {}},
    },
}
ASSERTIONS = {
    'type': 'array',
    'minItems': 1,
    'items': {
        'type': 'object',
        'required': ['path', 'equals'],
        'additionalProperties': False,
        'properties': {'path': {'type': 'string', 'pattern': '^(/.*)?$'}, 'equals': {}},
    },
}
CHECK_SCHEMA = {
    'oneOf': [
        {
            'type': 'object',
            'required': ['kind', 'source'],
            'properties': {
                'kind': {'const': 'python_check'},
                'source': {'type': 'string', 'minLength': 1, 'format': 'python-source'},
                'entrypoint': {'type': 'string', 'minLength': 1},
                'call_style': {'enum': ['kwargs', 'args', 'value']},
                'args': {},
                'requests': REQUESTS,
            },
        },
        {
            'type': 'object',
            'required': ['component_id'],
            'properties': {
                'component_id': {'type': 'string', 'minLength': 1},
                'args': {},
                'requests': REQUESTS,
                'expected': {},
                'assertions': ASSERTIONS,
            },
            'not': {'properties': {'kind': {'const': 'python_check'}}, 'required': ['kind']},
        },
    ]
}
RECORD_SCHEMA = {
    'type': 'object',
    'properties': {
        'preservation_checks': {'type': 'array', 'items': CHECK_SCHEMA},
        'structural_checks': {
            'type': 'array',
            'items': {
                'type': 'object',
                'required': ['kind', 'caller', 'callee'],
                'properties': {
                    'kind': {'enum': ['calls', 'absent_call']},
                    'caller': {'type': 'string'},
                    'callee': {'type': 'string'},
                },
            },
        },
    },
}


def diagnostics(proposal, old_components=()):
    """Return actionable protocol errors without executing candidate code."""
    issues = []
    for error in Draft202012Validator(RECORD_SCHEMA).iter_errors(proposal):
        issues.append(
            {
                'path': '/' + '/'.join(map(str, error.absolute_path)),
                'error': error.message,
                'supported_checks': 'Invocation {component_id,args,requests,expected or assertions:[{path,equals}]} or {kind:python_check,source,entrypoint:check}. Python checks import candidate functions, use ordinary mocks and return True after assertions.',
            }
        )
    if issues:
        return issues
    for index, check in enumerate(proposal.get('preservation_checks', [])):
        if check.get('kind') == 'python_check':
            try:
                python_backend.parse(check['source'])
            except (SyntaxError, ValueError, TypeError) as error:
                issues.append(
                    {
                        'fixture_index': index,
                        'kind': 'invalid_check_source',
                        'error': str(error),
                    }
                )
        elif (
            not ({'expected', 'assertions'} & set(check))
            and check['component_id'] not in old_components
        ):
            issues.append(
                {
                    'fixture_index': index,
                    'error': 'New-component fixture needs exact expected or typed assertions; no old implementation supplies an oracle.',
                }
            )
    return issues


def run_python_check(controller, check):
    """Run a temporary normal Python check; never publish it or infer task pass."""
    source = check['source']
    name = 'srcheck' + digest(source)[:24]
    if name in controller.state.candidate:
        raise ValueError('Check identity collides with candidate')
    references = python_backend.references(source, name)
    component = {
        'id': name,
        'language': python_backend.PROFILE,
        'source': source,
        'hash': digest(source),
        'dependencies': references['calls'],
        'contract': {},
        'interface': {
            'entrypoint': check.get('entrypoint', 'check'),
            'call_style': check.get('call_style', 'kwargs'),
            'host_bindings': {},
            'args_schema': {},
        },
    }
    try:
        invocation = controller.run_fixture(
            {**controller.state.candidate, name: component},
            name,
            check.get('args', {}),
            check.get('requests', []),
        )
    except (python_backend.FixtureEvidenceMissing, python_backend.FixtureExecutionError):
        raise
    except Exception as error:
        from ..execution.executor import exception_snapshot

        raise python_backend.FixtureExecutionError(exception_snapshot(error)) from error
    return invocation, digest(check)
