"""Analyze the code affected by the reuse plan and proposed edits.

The affected set includes selected components, their callers and dependencies,
and components named by repair. Each analysis records the related source and
plan inputs so unchanged results can be reused during proposal revisions.
"""

import copy
import json
from ..library import digest

INSTRUCTIONS = (
    'Return component_ref, reads, writes, calls, state_dependencies, feasible_boundaries, '
    'affected_callers, preservation_obligations, unresolved_dependencies, evidence_refs. Inspect'
    ' supplied actual source.'
)


def refresh_analyses(controller, *, existing_only=False):
    """Recompute affected analyses whose source or relevant planning inputs changed."""
    state = controller.state
    refreshed = []
    for component_id in affected_components(controller):
        if (
            existing_only
            and component_id not in state.component_analyses
            and component_id not in controller.library.manifest['components']
        ):
            continue
        key, context = analysis_context(controller, component_id)
        previous = state.component_analyses.get(component_id)
        # Reuse the prior analysis when its source, interfaces, and relevant plan inputs still
        # match.
        if previous and previous.get('analysis_key') == key:
            continue
        analysis = controller.operator('Analyze', context)
        state.component_analyses[component_id] = {
            'source_hash': state.candidate[component_id]['hash'],
            'analysis_key': key,
            'plan_hash': digest(state.reuse_plan),
            'context_profile': state.context_profile,
            'analysis': analysis,
        }
        refreshed.append(component_id)
    return refreshed


def _mentions(controller, value, names):
    """Search a nested requirement value for one of the supplied names."""
    state = controller.state
    if isinstance(value, str):
        return value in names
    if isinstance(value, dict):
        return any((k in names or controller._mentions(v, names) for k, v in value.items()))
    if isinstance(value, list):
        return any((controller._mentions(v, names) for v in value))
    return False


def analysis_context(controller, name):
    """Return an analysis cache key and the component, related source, and plan inputs."""
    state = controller.state
    'Version analysis against relevant choices and actual dependency interfaces.'
    state = controller.state
    components = state.candidate
    plan = state.reuse_plan
    related = {name} | set(components[name].get('dependencies', []))
    # Include callers as well as dependencies when following an interface change.
    related.update((n for n, c in components.items() if name in c.get('dependencies', [])))
    while True:
        expanded = related | {
            d for n in related if n in components for d in components[n].get('dependencies', [])
        }
        if expanded == related:
            break
        related = expanded
    choices = {
        k: v for k, v in plan.get('choices', {}).items() if v.get('component_id') in related
    }
    requirements = [
        r
        for r in plan.get('requirements', [])
        if (r if isinstance(r, str) else r.get('id')) in choices
    ]
    # dependencies: dict[str, object] | list[object]; filter the representation supplied by
    # Plan.
    dependencies = plan.get('dependencies', {})
    if isinstance(dependencies, dict):
        dependencies = {k: v for k, v in dependencies.items() if k in choices or k in related}
    else:
        dependencies = [
            v for v in dependencies if controller._mentions(v, set(choices) | related)
        ]
    relevant = {'requirements': requirements, 'choices': choices, 'dependencies': dependencies}
    # signature: dict[str, dict], containing the versions and interfaces used by this analysis.
    signature = {
        n: {k: components[n].get(k) for k in ('hash', 'interface', 'contract', 'dependencies')}
        for n in sorted(related)
        if n in components
    }
    # Cache the plan fragment related to this component and its dependency interfaces.
    key = digest(
        {'plan': relevant, 'components': signature, 'context_profile': state.context_profile}
    )
    return (
        key,
        {
            'component': components[name],
            'plan': relevant,
            'catalog': controller.catalog(),
            'related_source': {
                n: components[n] for n in sorted(related) if n != name and n in components
            },
        },
    )


def affected_components(controller):
    """Expand the selected and diagnosed component set through callers and dependencies."""
    state = controller.state
    names = set(state.candidate)
    affected = {
        c.get('component_id') for c in state.reuse_plan.get('choices', {}).values()
    } & names
    affected.update(
        (
            e.get('target')
            for e in (state.proposal or {}).get('edits', [])
            if e.get('target') in names
        )
    )
    affected.update(
        (
            n
            for n, issues in (state.repair_diagnosis or {}).get('G', {}).items()
            if issues and n in names
        )
    )
    while True:
        expanded = affected | {
            n
            for n, c in state.candidate.items()
            if n in affected or set(c.get('dependencies', [])) & affected
        }
        expanded.update(
            (
                d
                for n in affected
                for d in state.candidate[n].get('dependencies', [])
                if d in names
            )
        )
        if expanded == affected:
            return sorted(affected)
        affected = expanded
