"""Analyze：分析复用计划涉及的现有组件、调用者和依赖。

分析缓存按源码、接口、相关计划与上下文版本标识。组件关系变化时重新计算
受影响范围；仍匹配同一分析键的记录可以继续复用。
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
    """Refresh implicated versions; proposal reentry does not analyze new drafts."""
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
        # 同一分析键包含相关接口和计划；命中时复用已经完成的语义分析。
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
    """Check whether requirement evidence refers to affected names."""
    state = controller.state
    if isinstance(value, str):
        return value in names
    if isinstance(value, dict):
        return any((k in names or controller._mentions(v, names) for k, v in value.items()))
    if isinstance(value, list):
        return any((controller._mentions(v, names) for v in value))
    return False


def analysis_context(controller, name):
    """Build the exact source/interface and relevant-plan cache identity."""
    state = controller.state
    'Version analysis against relevant choices and actual dependency interfaces.'
    state = controller.state
    components = state.candidate
    plan = state.reuse_plan
    related = {name} | set(components[name].get('dependencies', []))
    # 调用者和被调用者一起决定接口变化会影响哪些分析。
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
    dependencies = plan.get('dependencies', {})
    if isinstance(dependencies, dict):
        dependencies = {k: v for k, v in dependencies.items() if k in choices or k in related}
    else:
        dependencies = [
            v for v in dependencies if controller._mentions(v, set(choices) | related)
        ]
    relevant = {'requirements': requirements, 'choices': choices, 'dependencies': dependencies}
    signature = {
        n: {k: components[n].get(k) for k in ('hash', 'interface', 'contract', 'dependencies')}
        for n in sorted(related)
        if n in components
    }
    # 缓存按相关计划切片计算，计划中无关部分的变化与本分析分开。
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
    """Expand actual callers and dependencies without including unrelated skills."""
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
