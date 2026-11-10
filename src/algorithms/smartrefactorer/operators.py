"""Stage input construction, not model transport or a second state machine.

Each stage owns its instruction string. This module selects the relevant source
view and records raw operator outputs while the injected adapter owns prompts,
provider errors, context continuations and actual metering.
"""

import copy
from .library import digest
from .development import plan, analyze, propose, codegen, rewrite
from .repair import reflect, patch
from .refactor import detect
from .execution.profile import PROFILE_GUIDE as LANGUAGE

ROLE_GUIDES = {
    'Plan': plan.INSTRUCTIONS,
    'Analyze': analyze.INSTRUCTIONS,
    'Propose': propose.INSTRUCTIONS,
    'CodeGen': codegen.INSTRUCTIONS,
    'Rewrite': rewrite.INSTRUCTIONS,
    'Reflect': reflect.INSTRUCTIONS,
    'Patch': patch.INSTRUCTIONS,
    'Detect': detect.INSTRUCTIONS,
}


def operator(controller, role, context):
    """Invoke a declared stage contract and retain its exact output history."""
    state = controller.state
    if role == 'Propose':
        context = {
            **context,
            'storage_contract': {
                'library_exports': 'Explicit list of components to retain as reusable skills; [] is valid. The task execution entry need not be exported. Preserve existing committed skills and include exported helpers dependencies. Do not turn supplied host admission/recovery into new skills.'
            },
        }
    if role in ('CodeGen', 'Rewrite', 'Patch') and 'candidate' in context:
        context = copy.deepcopy(context)
        all_components = context['candidate']
        target = (context.get('edit') or {}).get('target') or (
            context.get('component') or {}
        ).get('id')
        proposal = context.get('proposal') or {}
        edit = context.get('edit') or {}
        needed = {target} | set((all_components.get(target) or {}).get('dependencies', []))
        needed.update(
            (
                e['target']
                for e in proposal.get('edits', [])
                if e.get('id') in edit.get('prerequisite_steps', [])
            )
        )
        context['component_catalog'] = {
            n: {k: c.get(k) for k in ('id', 'hash', 'interface', 'contract', 'dependencies')}
            for n, c in all_components.items()
        }
        context['candidate'] = {n: c for n, c in all_components.items() if n in needed}
        context['source_access'] = (
            'Other complete sources are available with read_source. Full proposal and prior outputs are readable through development_history.'
        )
        if proposal:
            context['proposal'] = {
                **proposal,
                'preservation_checks': [
                    f
                    for f in proposal.get('preservation_checks', [])
                    if f.get('component_id') in needed
                ],
            }
    if role == 'Propose':
        context = {**context, 'operator_record_schema': propose.RECORD_SCHEMA}
    profile = LANGUAGE
    if role == 'Propose':
        profile += ' Return library_exports as an explicit list, or [] for task-only code. task_entry.argument_bindings is a JSON invocation template: {"$public":"/path"} reads a supplied public JSON Pointer, {"$evidence":{"id":"64hex","path":"/path"}} reads exact evidence, and {"$literal":value} escapes reserved syntax. Other literals remain literal. No implicit runtime.* values exist. Host callable parameters are declared separately in the component interface.host_bindings.'
    value = controller.ask(role, ROLE_GUIDES[role] + '\n' + profile, context)
    state.operator_history.append(
        {'role': role, 'input_hash': digest(context), 'output': value}
    )
    return value
