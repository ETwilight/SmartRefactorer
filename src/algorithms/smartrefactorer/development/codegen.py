"""Generate missing components and apply delivered component versions.

Each delivery contains source, interface, contract, target ID, and expected
source hash. Applying it updates the candidate and clears that component's
analysis so the next analysis reflects the replacement.
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
    """Check a delivery, parse its source, and replace the named candidate component."""
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
    # deps: dict with component call IDs, call-site IDs, and a dynamic-call flag.
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
    # The replacement changes the source version; refresh its dependency analysis on the next
    # pass.
    state.component_analyses.pop(target, None)


def edit_context(controller, edit, public):
    """Collect the edit, full proposal, reuse plan, candidate, and public task for
    generation.
    """
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
    """Request source and metadata for a component being added to the candidate."""
    if edit['target'] in controller.state.candidate:
        raise ValueError('Component name collision')
    return controller.operator('CodeGen', edit_context(controller, edit, public))
