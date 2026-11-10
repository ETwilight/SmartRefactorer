"""Propose：把复用计划与组件分析整理为协调的编辑方案。

方案声明编辑顺序、组件前置版本、调用参数、行为保持与结构检查，以及
任务入口和库导出集合。invocation_diagnostics 检查这些描述的可执行性。
"""

from ..execution.bindings import validate_template, BindingError
from ..execution.legacy_step import validate_transition
from ..execution.python_backend import PROFILE
from .check_contracts import RECORD_SCHEMA, diagnostics as check_diagnostics


def propose(controller, public):
    """Return a proposal against the current library and retained partial edits."""
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
    """Reject invalid explicit bindings before code generation, without losing edits."""
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
