"""Detect：从已发布组件的差异、调用结构与执行证据中提出维护机会。

返回机会的目标、操作族、受影响组件和证据引用；maintain 据此复用开发流程。
"""


def detect(controller):
    """Inspect unreviewed structural evidence; absence is not a forced refactor."""
    state = controller.state
    result = controller.operator(
        'Detect',
        {
            'candidate': controller.library.manifest['components'],
            'changes': state.publication_delta,
            'proposal': state.proposal,
            'checks': state.validation_evidence,
            'execution': state.execution.last_result,
            'accepted': state.accepted_task_receipt,
        },
    )
    return result.get('opportunity')


INSTRUCTIONS = (
    'Return opportunity (null or {goal, operation, target_refs, evidence_refs, '
    'affected_components}). Base structural opportunity on actual changed source/call relations '
    'and evidence. No invented future tasks.'
)
