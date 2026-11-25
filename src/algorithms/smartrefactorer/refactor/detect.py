"""Look for a maintenance goal after accepted components are published.

Detect reads the publication changes, proposal, checks, execution, and task
acceptance. A returned opportunity supplies the next maintenance goal; an empty
opportunity finishes the review for this task.
"""


def detect(controller):
    """Return a proposed maintenance opportunity from the latest publication evidence."""
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
