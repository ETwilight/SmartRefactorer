"""Plan：把任务需求映射为调用、提取或新增组件的复用选择。

先按当前任务与诊断检索候选，再按需要补读具体源码，最后校验需求身份
和引用版本，将本次有效计划交给 Analyze。
"""

import copy
import json
from ..library import digest

INSTRUCTIONS = (
    'Treat request admission, receipt retention/recovery, budget and persistence as supplied '
    'host capabilities, not new learned-skill requirements, unless the actual task explicitly '
    'asks to implement such a protocol. Obtain missing evidence through read interfaces rather '
    'than generating a replacement bridge. Decompose the actual supplied public task, not the '
    'bookkeeping work of recovering a plan. Requirements use stable compact IDs. You may revise '
    'uncommitted candidate drafts and their interfaces under the current task authorization; do '
    'not invent human/coordinator approval requirements. Transient '
    'task/session/world/activation/request identities belong to explicit invocation data, not '
    'reusable source/interface constants. Preserve unaffected valid choices. Return requirements'
    ' (list), choices (mapping requirement to {kind: call|extract|new, component_id, '
    'source_hash, capability}), dependencies. Preserve unaffected existing choices. Refer only '
    'to catalog versions. You may request omitted source using read_ids.'
)


def validate_plan(controller, plan):
    """Validate stable requirement IDs and exact existing source versions."""
    state = controller.state
    if (
        not isinstance(plan, dict)
        or not isinstance(plan.get('requirements'), list)
        or (not plan['requirements'])
        or (not isinstance(plan.get('choices'), dict))
    ):
        raise ValueError(
            'Plan requires nonempty task requirements and an explicit choices mapping'
        )
    import re

    ids = []
    for requirement in plan['requirements']:
        rid = (
            requirement
            if isinstance(requirement, str)
            else requirement.get('id') if isinstance(requirement, dict) else None
        )
        if not isinstance(rid, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_.-]{0,79}', rid):
            raise ValueError('Requirement IDs must be compact stable identifiers')
        if rid in ids:
            raise ValueError('Duplicate requirement ' + rid)
        ids.append(rid)
        if rid not in plan['choices']:
            raise ValueError('Missing reuse choice for ' + rid)
    for choice in plan['choices'].values():
        if not isinstance(choice, dict) or choice.get('kind') not in ('call', 'extract', 'new'):
            raise ValueError('Invalid reuse choice')
        if choice['kind'] in ('call', 'extract'):
            component = state.candidate[choice['component_id']]
            if choice.get('source_hash') != component['hash']:
                raise ValueError('Stale reuse source')


def plan(controller, facts):
    """Request a reuse plan and retain the preceding valid plan on rejection."""
    state = controller.state
    task = facts.get('active_task') or {}
    repair = state.repair_diagnosis or {}
    query = {
        'goal': facts['goal'],
        'task': {k: task.get(k) for k in ('id', 'title', 'goal')},
        'repair_locations': sorted(repair.get('G', {})),
        'issue_kinds': sorted(
            {i.get('kind', '') for issues in repair.get('G', {}).values() for i in issues}
        ),
        'opportunity': state.refactor_opportunity,
    }
    state.retrieval = controller.retrieve(query)
    previous = copy.deepcopy(state.reuse_plan)
    proposed = controller.operator(
        'Plan',
        {
            'public': facts,
            'catalog': controller.catalog(),
            'retrieval': state.retrieval,
            'previous': previous,
            'last_valid_plan': state.last_valid_plan,
            'repair': state.repair_diagnosis,
        },
    )
    # Plan 可以按组件身份补读被初次检索省略的源码，再形成最终复用选择。
    for identifier in proposed.get('read_ids', []):
        state.retrieval['selected'][identifier] = state.candidate[identifier]
    if proposed.get('read_ids'):
        proposed = controller.operator(
            'Plan',
            {
                'public': facts,
                'catalog': controller.catalog(),
                'retrieval': state.retrieval,
                'previous': previous,
                'draft': proposed,
                'repair': state.repair_diagnosis,
            },
        )
    try:
        controller.validate_plan(proposed)
    except (ValueError, KeyError, TypeError) as exc:
        state.plan_rejection = {
            'error': str(exc),
            'proposed_plan_hash': digest(proposed),
            'retained_plan_hash': digest(previous),
        }
        raise
    # 经过版本与需求校验后，当前计划和最近有效计划一起更新。
    state.reuse_plan = proposed
    state.last_valid_plan = copy.deepcopy(proposed)
    state.operator_history.append(
        {
            'kind': 'plan.accepted',
            'plan_hash': digest(proposed),
            'plan': copy.deepcopy(proposed),
        }
    )
