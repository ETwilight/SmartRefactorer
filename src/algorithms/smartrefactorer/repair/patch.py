"""Patch：逐个修正 Reflect 指定的组件，再返回 Validate。

repair_targets 给出组件顺序，repair_cursor 标记已完成的交付。
每次应用绑定组件版本，候选中其余已完成工作随状态一起保存。
"""

import copy
from ..state import Phase


def patch_components(controller):
    """Apply only the attributed components, resuming the saved repair cursor."""
    state = controller.state
    issues = state.repair_diagnosis['G']
    while state.repair_cursor < len(state.repair_targets):
        # repair_cursor 指向当前待修组件，前面已经应用的补丁保留在候选中。
        component_id = state.repair_targets[state.repair_cursor]
        if component_id not in state.candidate:
            return Phase.PROPOSE
        component = state.candidate[component_id]
        delivery = controller.operator(
            'Patch',
            {
                'component': component,
                'issues': issues[component_id],
                'proposal': state.proposal,
                'candidate': state.candidate,
                'checks': state.validation_evidence,
                'suspended_execution': state.execution.suspended_legacy,
            },
        )
        if delivery.get('delivery_status') == 'blocked':
            state.blocked_delivery = {
                'resume_phase': 'patch',
                'target': component_id,
                'record': copy.deepcopy(delivery),
            }
            return Phase.BLOCKED
        if delivery.get('conflict'):
            return Phase.PROPOSE
        # 补丁按修复开始时读取的组件哈希应用，然后保存完成进度。
        controller.apply(delivery, component_id, component['hash'])
        state.repair_cursor += 1
        controller.save()
    return Phase.VALIDATE


INSTRUCTIONS = (
    'Return target, expected_hash, source, interface, contract, or conflict (explain required '
    'proposal revision). Correct the assigned implementation and retain valid behavior.'
)
