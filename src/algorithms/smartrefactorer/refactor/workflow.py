"""Develop and publish a maintenance change to the committed skill library.

The accepted task state is archived before a maintenance candidate is created.
Its goal passes through the same planning, editing, validation, and repair stages.
Publication requires both behavior-preservation and structural checks.
"""

import copy
from ..library import digest
from ..state import Phase
from .detect import detect


def maintain(controller):
    """Advance maintenance and return True when its review or publication is complete."""
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
        # Start the maintenance candidate from the committed library after archiving task
        # development.
        state.candidate = copy.deepcopy(controller.library.manifest['components'])
        state.is_maintenance = True
        state.base_library_hash = digest(controller.library.manifest)
        state.reuse_plan = None
        state.proposal = None
        state.component_analyses = {}
        state.validation_evidence = []
        controller.transition(Phase.IDLE)
        controller.save()
    # Use the detected structural goal as the input to the existing development stages.
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
    # Publish the maintenance candidate after both check groups have passed.
    controller.library.commit(
        state.base_library_hash, state.candidate, state.validation_evidence, state.proposal
    )
    state.is_maintenance = False
    controller.transition(Phase.IDLE)
    controller.save()
    return True
