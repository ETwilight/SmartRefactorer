"""Turn reuse choices and dependency findings into an implementation proposal.

The proposal specifies edit order, source versions, task arguments, preservation
and structural checks, and library exports. Its invocation and check records
are inspected before CodeGen or Rewrite starts applying edits.
"""

from ..execution.bindings import validate_template, BindingError
from ..execution.legacy_step import validate_transition
from ..execution.python_backend import PROFILE
from .check_contracts import RECORD_SCHEMA, diagnostics as check_diagnostics


def propose(controller, public):
    """Request coordinated edits for the current plan, analyses, and partially edited
    candidate.
    """
    state = controller.state
    proposal = controller.operator(
        'Propose',
        {
            'public': public,
            'goal': public['goal'],
            'plan': state.reuse_plan,
            'analyses': state.component_analyses,
            'candidate': state.candidate,
            'base_version': controller.library.manifest['version'],
            'previous': state.proposal,
            'repair': state.repair_diagnosis,
            'proposal_contract_diagnostics': (
                check_diagnostics(state.proposal, controller.library.manifest['components'])
                if state.proposal
                else []
            ),
        },
    )
    if not (proposal.get('blockers') and not proposal.get('edits')):
        if proposal.get('base_version') != controller.library.manifest['version']:
            raise ValueError('Stale proposal parent')
    return proposal


def invocation_diagnostics(controller):
    """Collect malformed argument bindings and check records for proposal revision."""
    state = controller.state
    diagnostics = check_diagnostics(state.proposal, controller.library.manifest['components'])
    entry = state.proposal.get('task_entry')
    if entry:
        try:
            validate_template(entry.get('argument_bindings', {}))
        except BindingError as error:
            diagnostics.append(
                {'kind': 'invalid_argument_binding', 'error': str(error), 'task_entry': entry}
            )
    if diagnostics:
        return diagnostics
    for index, fixture in enumerate(state.proposal.get('preservation_checks', [])):
        component = state.candidate.get(fixture.get('component_id'), {})
        if 'expected' not in fixture or not component or component.get('language') == PROFILE:
            continue
        expected = fixture['expected']
        if isinstance(expected, dict) and (
            'assertions' in expected or 'state_assertions' in expected
        ):
            continue
        try:
            validate_transition(expected)
        except (ValueError, TypeError) as error:
            diagnostics.append(
                {'fixture_index': index, 'error': str(error), 'expected': expected}
            )
    return diagnostics


INSTRUCTIONS = (
    'Coordinate the supplied full task plan, including missing behavior. Preserve unrelated '
    'valid edits. Return id, revision, base_version, edits (ordered '
    '{id,target,operation:new|rewrite,expected_hash,instructions,prerequisite_steps}), '
    'task_entry {component_id,argument_bindings}, preservation_checks '
    'as {component_id,args,expected} or {component_id,args,assertions:[{path:JSON_POINTER,equals:JSON_VALUE}]}, '
    'optionally requests:[{request:OBJECT,feedback:JSON_VALUE}]. For complex tests use '
    '{kind:python_check,source:PYTHON_SOURCE,entrypoint:check}: an ordinary Python function '
    'imports candidate functions, supplies ordinary fake request callables, asserts behavior '
    'and returns True. No live environment is injected. Narrative text is not executable. '
    'Python checks supplement rather than replace valid-old-input preservation fixtures. '
    'Return structural_checks ({kind:calls|absent_call,caller,callee}) and library_exports. '
    'Coordinate actual callable interfaces and affected callers. New identifiers cannot '
    'overwrite existing skills. Fixtures concern complete observable calls, not identical '
    'internal transitions. Return concrete blockers if necessary. Task-only code need not become'
    ' a long-term skill. Do not invent approval requirements for ordinary draft edits.'
)
