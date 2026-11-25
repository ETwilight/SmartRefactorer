"""Rewrite an existing component as part of the coordinated proposal.

The current component must match the edit's expected source hash. Rewrite gets
the same proposal and candidate context as CodeGen, then returns a delivery for
apply_component to install.
"""

from .codegen import edit_context


def rewrite(controller, edit, public):
    """Request a replacement for the component version named by this edit."""
    component = controller.state.candidate[edit['target']]
    if component['hash'] != edit['expected_hash']:
        raise ValueError('Stale rewrite source')
    return controller.operator('Rewrite', edit_context(controller, edit, public))


INSTRUCTIONS = 'Return target, expected_hash, source, interface, contract. Apply assigned coordinated migration with preserved behavior. Retain unrelated edits.'
