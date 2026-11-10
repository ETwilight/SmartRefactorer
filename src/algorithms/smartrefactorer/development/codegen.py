"""CodeGen：按协调后的方案生成新组件并应用到候选版本。

交付包含源码、接口、契约和目标版本；apply_component 更新候选及其依赖，
随后由 controller 推进编辑游标与 Validate。
"""

import copy
import json
from ..library import digest
from ..execution import python_backend
from ..execution.legacy_step import parse, references

INSTRUCTIONS = (
    'Return target, expected_hash (null for new), source, interface, contract. Generate the '
    'assigned new component with declared explicit bindings. Keep agreed interfaces.'
)


def apply_component(controller, out, target, expected):
    """Apply an exact-version delivery without altering the source text."""
    state = controller.state
    if out.get('conflict'):
        raise ValueError('Interface conflict: ' + str(out['conflict']))
    if (
        not isinstance(out.get('source'), str)
        or not isinstance(out.get('interface'), dict)
        or (not isinstance(out.get('contract'), dict))
    ):
        raise ValueError('Component delivery requires source text and object metadata')
    if out['target'] != target or out.get('expected_hash') != expected:
        raise ValueError('Edit binding mismatch')
    normal = bool(out['interface'].get('entrypoint'))
    backend = python_backend if normal else None
    (backend.parse if backend else parse)(out['source'])
    deps = backend.references(out['source'], target) if backend else references(out['source'])
    if normal:
        declared = out['interface'].get('dependencies', [])
        if not isinstance(declared, list) or any(
            (not isinstance(name, str) for name in declared)
        ):
            raise ValueError('Declared dependencies must be component IDs')
        deps['calls'] = sorted(set(deps['calls']) | set(declared))
    if not normal and deps['unresolved_dynamic_calls']:
        raise ValueError('Dynamic legacy helper binding requires analysis')
    state.candidate[target] = {
        'id': target,
        'language': python_backend.PROFILE if normal else 'pure-python-step-v1',
        'source': out['source'],
        'hash': digest(out['source']),
        'interface': out['interface'],
        'contract': out['contract'],
        'dependencies': deps['calls'],
    }
    # 新源码替换旧版本后，该组件原有分析进入重新计算路径。
    state.component_analyses.pop(target, None)


def edit_context(controller, edit, public):
    """Keep code generation inputs bound to the full coordinated proposal."""
    state = controller.state
    return {
        'public': public,
        'edit': edit,
        'goal': public['goal'],
        'plan': state.reuse_plan,
        'proposal': state.proposal,
        'candidate': state.candidate,
    }


def generate(controller, edit, public):
    """Generate a new component, never overwrite an existing identity."""
    if edit['target'] in controller.state.candidate:
        raise ValueError('Component name collision')
    return controller.operator('CodeGen', edit_context(controller, edit, public))
