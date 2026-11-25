"""Patch the components selected by Reflect, then validate the candidate again.

repair_targets orders the component IDs and repair_cursor marks the next one.
A delivered patch is applied to the version read for that edit, and the updated
candidate and cursor are saved together.
"""

import copy
from ..state import Phase


def patch_components(controller):
    """Continue the repair list and return the phase reached after applying its patches."""
    state = controller.state
    # issues: dict[str, list[dict]], grouping Reflect's requested fixes by component ID.
    issues = state.repair_diagnosis['G']
    while state.repair_cursor < len(state.repair_targets):
        # Earlier targets already have their applied patches in the candidate.
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
        # Apply against the source version read above, then save the advanced repair cursor.
        controller.apply(delivery, component_id, component['hash'])
        state.repair_cursor += 1
        controller.save()
    return Phase.VALIDATE


INSTRUCTIONS = (
    'Return target, expected_hash, source, interface, contract, or conflict (explain required '
    'proposal revision). Correct the assigned implementation and retain valid behavior.'
)
