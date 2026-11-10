"""Reflect：从执行反馈、检查结果与失败记录定位需要修改的范围。

G 记录问题和 revision_scope，L 记录相关来源。controller 根据诊断进入
Plan、Propose 或组件 Patch，候选源码由后续阶段修改。
"""


def reflect(controller, public):
    """Diagnose observed evidence, preserving unaffected candidate work."""
    state = controller.state
    return controller.operator(
        'Reflect',
        {
            'public': public,
            'goal': public['goal'],
            'plan': state.reuse_plan,
            'proposal': state.proposal,
            'candidate': state.candidate,
            'checks': state.validation_evidence,
            'execution': state.execution.last_result,
            'suspended_execution': state.execution.suspended_legacy,
            'failures': state.failure,
        },
    )


INSTRUCTIONS = (
    'Return G (mapping component to issue lists, each with kind, repair_requirements, '
    'revision_scope: component|proposal|reuse_plan), L (mapping component to source references).'
    ' Attribute from actual observed evidence, not unexecuted code. Preserve unaffected changes.'
)
