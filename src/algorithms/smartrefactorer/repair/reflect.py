"""Locate the changes needed after a failed check or task execution.

Reflect receives the reuse plan, proposal, current source, validation results,
execution record, and failure. Its issue groups choose component patches,
proposal revision, or replanning; its locations identify the related source.
"""


def reflect(controller, public):
    """Request a diagnosis using the current failure and the evidence collected so far."""
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
