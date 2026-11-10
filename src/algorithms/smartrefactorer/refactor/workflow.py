"""维护重构：Detect → 复用 Develop/repair → 行为与结构检查 → 发布。

维护以当前正式库为父版本，并保存先前的任务开发状态；机会处理完成后
记录库版本并将控制权交回宿主。
"""

import copy
from ..library import digest
from ..state import Phase
from .detect import detect


def maintain(controller):
    """Advance an optional structural opportunity using existing development stages."""
    state = controller.state
    if not state.is_maintenance:
        delta = state.publication_delta
        if delta is not None and not delta['changed'] and not delta['removed']:
            controller.transition(Phase.IDLE)
            return True
        state.refactor_opportunity = detect(controller)
        if not state.refactor_opportunity:
            controller.transition(Phase.IDLE)
            return True
        if controller.archive_accepted_state is not None:
            state.task_development_archive = controller.archive_accepted_state()
        # 维护从当前正式库创建候选，先前任务的开发状态已单独归档。
        state.candidate = copy.deepcopy(controller.library.manifest['components'])
        state.is_maintenance = True
        state.base_library_hash = digest(controller.library.manifest)
        state.reuse_plan = None
        state.proposal = None
        state.component_analyses = {}
        state.validation_evidence = []
        controller.transition(Phase.IDLE)
        controller.save()
    # 维护目标复用同一套 Plan、Develop 与 repair 阶段。
    outcome = controller.advance({'goal': state.refactor_opportunity['goal']})
    if not outcome.ready_to_execute:
        return False
    if (
        not state.proposal.get('preservation_checks')
        or not state.proposal.get('structural_checks')
        or state.proposal.get('task_entry')
    ):
        state.validation_evidence.append(
            {
                'id': 'maintenance-verification',
                'status': 'PENDING',
                'observed': 'Executable preservation and structural checks required; no task entry',
            }
        )
        controller.transition(Phase.REPAIR)
        controller.save()
        return False
    # 行为保持和结构目标均有检查证据后，维护候选进入库事务。
    controller.library.commit(
        state.base_library_hash, state.candidate, state.validation_evidence, state.proposal
    )
    state.is_maintenance = False
    controller.transition(Phase.IDLE)
    controller.save()
    return True
