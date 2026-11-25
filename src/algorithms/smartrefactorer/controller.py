"""Develop task code from reusable skills, repair failures, and publish accepted changes.

Plan chooses calls, extractions, and new components. Analyze follows the affected
code; Propose orders the changes. CodeGen and Rewrite apply them before Validate.
Reflect uses failed checks and execution feedback to choose how far to revise.
After acceptance, Detect can start maintenance through the same development loop.
"""

import copy
from .library import digest
from .state import AlgorithmState, CheckpointView, Phase, StageResult
from . import retrieval
from .development import plan, analyze, propose, codegen, rewrite, validate
from .development.check_contracts import diagnostics as check_contract_diagnostics
from .repair import reflect, patch
from .operators import operator as invoke_operator, ROLE_GUIDES, LANGUAGE
from .execution import python_backend


class Controller:
    """Coordinate development, execution feedback, and changes to the skill library.

    library stores the committed Library. state is an AlgorithmState holding the current
    candidate, reuse plan, proposal, checks, and execution records. s is a CheckpointView
    of that same state, using the field names written to checkpoint files.

    ask(role, instructions, context) asks the operator specified by role (see ROLE_GUIDES in operators.py) and
    fueled by the context about the instructions and returns the corresponding result;
    embed(texts) embeds the query (of the skill we currently needs) and the {id, interface, contract} of each skill into a vector.
    The adapter supplies both callbacks and persists state through save().
    advance() runs development until the next execution or repair boundary.
    The adapter executes the candidate and brings its result back to this controller.

    Example, after the adapter has supplied the library and callbacks::

        controller = Controller(library, ask, embed)
        outcome = controller.advance(public)
        if outcome.ready_to_execute:
            entry = controller.state.proposal['task_entry']

    entry names the proposed task call. At this point its development checks have
    passed; executing it and obtaining the task verdict are the next operations.
    """

    def __init__(
        self,
        library,
        ask,
        embed,
        state=None,
        top_k=6,
        run_fixture=None,
        save=None,
        archive_accepted_state=None,
    ):
        """Start from the supplied checkpoint or a copy of the committed library."""

        self.library = library                                              # the committed library
        self.ask = ask                                                      # ask(role, instructions, context)
        self.embed = embed                                                  # string list: ['{"query": ...}', '{"id": ..., "interface": ..., "contract": ...}' (of skill A), ... (of skill B), ...] => vector
        self.top_k = top_k                                                  # keep the top_k skills we retrieved through query
        self.run_fixture = run_fixture or python_backend.fixture            # will rewrite this logic
        self._save_callback = save
        self.archive_accepted_state = archive_accepted_state
        self.s = state or {
            'phase': 'idle',
            'parent': digest(library.manifest),
            'candidate': copy.deepcopy(library.manifest['components']),
            'p': None,
            'A': {},
            'P': None,
            'E': [],
            'r': None,
            'o': None,
            'history': [],
            'frames': [],
            'pending': None,
        }

    @property
    def s(self):
        """Return the checkpoint dictionary linked to the current algorithm state."""

        return self._checkpoint

    @s.setter
    def s(self, document):
        """Load checkpoint fields into a fresh AlgorithmState."""

        self.state = AlgorithmState()
        # document: dict[str, object]; s exposes these fields through the new typed state.
        self._checkpoint = CheckpointView(self.state, document)

    def save(self):
        """Call the persistence callback after a completed state update."""

        if self._save_callback is not None:
            self._save_callback()

    def transition(self, phase):
        """Set the next development phase."""
        self.state.phase = Phase(phase)

    def operator(self, role, context):
        """Call the named operator with its role instructions and current context."""

        return invoke_operator(self, role, context)

    def retrieve(self, query):
        """Rank candidate skills for the supplied task or repair query."""

        return retrieval.retrieve(self, query)

    def catalog(self):
        """Return component IDs, versions, interfaces, contracts, and dependencies."""

        return {
            name: {
                key: component.get(key)
                for key in ('id', 'hash', 'interface', 'contract', 'dependencies')
            }
            for name, component in self.state.candidate.items()
        }

    def validate_plan(self, value):
        """Check requirement IDs, reuse choices, and referenced component versions."""

        return plan.validate_plan(self, value)

    def plan(self, public):
        """Store a reuse plan and continue with dependency analysis."""

        plan.plan(self, public)
        self.transition(Phase.ANALYZE)

    def analysis_context(self, component_id):
        """Collect a component, its dependencies, callers, and current plan."""

        return analyze.analysis_context(self, component_id)

    def affected_components(self):
        """Find the existing components selected for reuse or migration."""

        return analyze.affected_components(self)

    def _mentions(self, value, names):
        """Find references to the supplied component names inside a nested value."""

        return analyze._mentions(self, value, names)

    def apply(self, delivery, component_id, expected_hash):
        """Apply one component delivery against the expected source version."""

        codegen.apply_component(self, delivery, component_id, expected_hash)

    def validate(self):
        """Run the proposal checks against the current candidate."""

        return validate.validate(self)

    def advance(self, public):
        """Continue the saved development phase and return a StageResult.

        Completed edits stay in the candidate. A failed check enters repair; passing
        checks return ready_to_execute so the adapter can invoke the task entry.
        """
        state = self.state
        # Continue the saved phase when a reuse plan exists; PLAN starts a new choice of skills.
        if not state.reuse_plan or state.phase == Phase.PLAN:
            self.plan(public)
        if state.phase in (Phase.ANALYZE, Phase.PROPOSE):
            # Refresh analyses whose source, interfaces, or related plan inputs have changed.
            analyze.refresh_analyses(self, existing_only=state.phase == Phase.PROPOSE)
            self.transition(Phase.PROPOSE)
        if state.phase == Phase.PROPOSE:
            state.proposal = propose.propose(self, public)
            # Record proposal blockers as pending evidence for the next repair pass.
            if state.proposal.get('blockers') and not state.proposal.get('edits'):
                state.validation_evidence = [
                    {
                        'id': 'proposal-blocker-' + str(index),
                        'candidate_hash': digest(state.candidate),
                        'status': 'PENDING',
                        'observed': blocker,
                    }
                    for index, blocker in enumerate(state.proposal['blockers'])
                ]
                self.transition(Phase.REPAIR)
                return StageResult(state.phase)
            state.edit_cursor = 0
            self.transition(Phase.EDIT)
        if state.phase == Phase.EDIT:
            # Correct invocation and check descriptions in Propose before editing components.
            diagnostics = propose.invocation_diagnostics(self)
            if diagnostics:
                state.proposal_diagnostics = diagnostics
                state.repair_diagnosis = {
                    'G': {
                        'proposal': [
                            {
                                'kind': 'invalid_proposal_contract',
                                'revision_scope': 'proposal',
                                'repair_requirements': [
                                    'Correct invalid invocation bindings or exact fixture transitions before further code generation. Use explicit public/evidence references or literal JSON values, and preserve valid candidate edits and unchanged plan.'
                                ],
                                'diagnostics': diagnostics,
                            }
                        ]
                    },
                    'L': {},
                }
                self.transition(Phase.PROPOSE)
                return StageResult(state.phase)
            self._implement_edits(public)
            if state.phase == Phase.BLOCKED:
                return StageResult(state.phase, blocked=True)
        if state.phase == Phase.VALIDATE:
            # Passing checks make this candidate version ready for execution.
            state.validation_evidence = self.validate()
            self.transition(
                Phase.READY
                if all(item['status'] == 'PASS' for item in state.validation_evidence)
                else Phase.REPAIR
            )
        if state.phase == Phase.REPAIR:
            self.repair(public)
        elif state.phase == Phase.PATCH:
            self.patch()
        return StageResult(
            state.phase,
            ready_to_execute=state.phase == Phase.READY,
            blocked=state.phase == Phase.BLOCKED,
        )

    def develop(self, public):
        """Return whether development has produced a candidate ready for execution."""
        return self.advance(public).ready_to_execute

    def _implement_edits(self, public):
        """Apply proposal edits in prerequisite order, saving after each delivery."""

        state = self.state
        # edits: list[dict]; each record names a target, source hash, operation, and
        # prerequisites.
        edits = state.proposal['edits']
        while state.edit_cursor < len(edits):
            edit = edits[state.edit_cursor]
            previous = state.candidate.get(edit['target'])
            # Each edit reads its source version; prerequisite_steps names the earlier edits it
            # needs.
            expected = previous['hash'] if previous else None
            if edit.get('expected_hash') != expected:
                raise ValueError('Stale edit source')
            completed = {item['id'] for item in edits[: state.edit_cursor]}
            if not set(edit.get('prerequisite_steps', [])) <= completed:
                raise ValueError('Unmet edit prerequisites')
            if edit['operation'] == 'new' and previous:
                raise ValueError('Component name collision')
            delivery = (
                rewrite.rewrite(self, edit, public)
                if previous
                else codegen.generate(self, edit, public)
            )
            if delivery.get('delivery_status') == 'blocked':
                state.blocked_delivery = {
                    'resume_phase': 'edit',
                    'edit_cursor': state.edit_cursor,
                    'record': copy.deepcopy(delivery),
                }
                self.transition(Phase.BLOCKED)
                return
            # Advance the cursor after applying the component so the saved state contains the
            # completed edit.
            self.apply(delivery, edit['target'], expected)
            state.edit_cursor += 1
            self.save()
        self.transition(Phase.VALIDATE)

    def repair(self, public):
        """Use Reflect to revise the reuse plan, proposal, or named components."""
        state = self.state
        state.repair_diagnosis = reflect.reflect(self, public)
        # issues: dict[str, list[dict]]; G groups diagnosed problems by component or proposal.
        issues = {
            name: values
            for name, values in state.repair_diagnosis.get('G', {}).items()
            if values
        }
        scopes = {issue.get('revision_scope') for values in issues.values() for issue in values}
        # Plan-level issues take priority, followed by proposal revisions and component patches.
        if 'reuse_plan' in scopes:
            self.transition(Phase.PLAN)
        elif 'proposal' in scopes or not issues:
            self.transition(Phase.PROPOSE)
        else:
            state.repair_targets = list(issues)
            state.repair_cursor = 0
            self.transition(Phase.PATCH)
            self.patch()

    def patch(self):
        """Patch the diagnosed components and select the following phase."""

        self.transition(patch.patch_components(self))

    def accepted(self, receipt):
        """Publish the candidate covered by the supplied task acceptance receipt."""
        return self.accept_task(receipt)

    def maintain(self):
        """Look for a structural improvement and advance its development."""
        from .refactor.workflow import maintain

        return maintain(self)

    def activate_task(self, binding, context_profile):
        """Start a task candidate from the committed library and bind its context."""
        self.s = {
            'phase': 'idle',
            'parent': digest(self.library.manifest),
            'candidate': copy.deepcopy(self.library.manifest['components']),
            'p': None,
            'A': {},
            'P': None,
            'E': [],
            'r': None,
            'o': None,
            'history': [],
            'frames': [],
            'pending': None,
            'binding': binding,
            'context_profile': context_profile,
        }

    def prepare_development(self, public):
        """Apply pending verdicts and diagnostics, then continue development."""
        state = self.state
        # Check the saved proposal against the current executable check schema.
        # A check-description error sends that proposal back to Propose.
        contract_issues = (
            check_contract_diagnostics(state.proposal, self.library.manifest['components'])
            if state.proposal
            else []
        )
        if contract_issues and state.phase not in (Phase.PLAN, Phase.ANALYZE, Phase.PROPOSE):
            state.proposal_diagnostics = contract_issues
            state.repair_diagnosis = {
                'G': {
                    'proposal': [
                        {
                            'kind': 'invalid_check_contract',
                            'revision_scope': 'proposal',
                            'repair_requirements': [
                                'Correct executable check records; preserve valid candidate sources. Use edits=[] if implementation changes are unnecessary.'
                            ],
                            'diagnostics': contract_issues,
                        }
                    ]
                },
                'L': {},
            }
            self.transition(Phase.PROPOSE)
            self.save()
        if state.awaiting_task_verdict:
            state.awaiting_task_verdict = False
            self.transition(Phase.REPAIR)
        if state.needs_replan:
            self.plan(public)
            state.needs_replan = False
            self.save()
        if state.failure and state.phase == Phase.READY:
            self.transition(Phase.REPAIR)
        outcome = self.advance(public)
        if outcome.ready_to_execute and (state.failure or {}).get('type', '').rsplit('.', 1)[
            -1
        ] in (
            'TypeError',
            'ValueError',
            'SyntaxError',
            'KeyError',
            'IndexError',
            'ZeroDivisionError',
            'OverflowError',
        ):
            self.s['last_resolved_development_failure'] = copy.deepcopy(state.failure)
            state.failure = None
        return outcome

    def record_development_failure(self, error):
        """Save a local development error for Reflect and enter repair."""
        self.state.failure = {'type': type(error).__name__, 'message': str(error)}
        self.transition(Phase.REPAIR)

    def record_execution_failure(self, error):
        """Mark the invocation failed and keep its record for repair."""
        self.state.failure = copy.deepcopy(error)
        invocation = self.state.execution.invocation
        if invocation is not None:
            invocation['status'] = 'failed'
            self.state.execution.recovery = copy.deepcopy(invocation)
        self.transition(Phase.REPAIR)

    def record_task_failure(self, receipt):
        """Save the task evaluator result and enter repair."""
        self.state.failure = copy.deepcopy(receipt)
        self.transition(Phase.REPAIR)

    def record_invocation_issue(self, error):
        """Send an argument-binding error back to Propose."""
        self.s['argument_binding_error'] = {'type': type(error).__name__, 'message': str(error)}
        self.state.repair_diagnosis = {
            'G': {
                'proposal': [
                    {
                        'kind': 'invalid_argument_binding',
                        'revision_scope': 'proposal',
                        'repair_requirements': [str(error)],
                    }
                ]
            },
            'L': {},
        }
        self.transition(Phase.PROPOSE)

    def preserve_legacy_interruption(self):
        """Build a recovery record from a saved instruction-frame execution."""
        execution = self.state.execution
        if execution.suspended_legacy and not execution.recovery:
            suspended = execution.suspended_legacy
            root = suspended['frames'][0]
            execution.recovery = {
                'invocation_id': root['invocation_id'],
                'component_id': root['component_id'],
                'arguments': copy.deepcopy(root['args']),
                'candidate_hash': suspended['candidate_hash'],
                'status': 'interrupted',
                'legacy_frames': copy.deepcopy(suspended['frames']),
            }

    def prepare_invocation(self, component_id, arguments, invocation_id, continuation_of=None):
        """Check the candidate version and arguments, then record a running call."""
        state = self.state
        recovery = state.execution.recovery
        if state.execution.pending_request:
            raise ValueError('An existing operation still needs reconciliation')
        if recovery and continuation_of != recovery['invocation_id']:
            raise ValueError(
                'An explicit continuation decision must identify the interrupted invocation'
            )
        candidate_hash = digest(state.candidate)
        if not state.validation_evidence or any(
            item.get('status') != 'PASS' or item.get('candidate_hash') != candidate_hash
            for item in state.validation_evidence
        ):
            raise ValueError('Execution requires current candidate checks')
        component = state.candidate[component_id]
        if component.get('language') != python_backend.PROFILE:
            raise ValueError(
                'Use the declared Python callable interface for a normal invocation'
            )
        from jsonschema import Draft202012Validator

        Draft202012Validator(component['interface'].get('args_schema', {})).validate(arguments)
        invocation = {
            'invocation_id': invocation_id,
            'component_id': component_id,
            'arguments': copy.deepcopy(arguments),
            'candidate_hash': candidate_hash,
            'status': 'running',
            'continuation_of': continuation_of,
        }
        state.execution.invocation = invocation
        state.execution.executed_candidate_hash = candidate_hash
        if recovery:
            state.execution.recovery_history.append(copy.deepcopy(recovery))
            state.execution.suspended_legacy = None
        state.execution.recovery = None
        return invocation

    def record_execution_finished(self, result):
        """Store the returned value and wait for the task evaluator verdict."""
        state = self.state
        state.execution.invocation['status'] = 'completed'
        state.execution.last_result = {
            'execution': 'completed',
            'result': result,
            'candidate_hash': state.execution.executed_candidate_hash,
        }
        state.awaiting_task_verdict = True

    def accept_task(self, receipt):
        """Publish accepted components and their dependencies, then enter Detect.

        The task receipt and passing development checks must refer to this candidate.
        library_exports selects which components are added to the committed library.
        """
        state = self.state
        if receipt['candidate_hash'] != digest(state.candidate):
            raise ValueError('Acceptance for stale candidate')
        # Store the task verdict before checking the publication evidence for the same
        # candidate.
        state.accepted_task_receipt = copy.deepcopy(receipt)
        if not state.validation_evidence or any(
            (
                e.get('status') != 'PASS' or e.get('candidate_hash') != digest(state.candidate)
                for e in state.validation_evidence
            )
        ):
            raise ValueError('Publication requires current version-bound passing checks')
        evidence = state.validation_evidence + [
            {
                'id': 'task-evaluator',
                'status': 'PASS',
                'receipt': receipt,
                'candidate_hash': receipt['candidate_hash'],
            }
        ]
        # exports: list[str] | None. None publishes the full candidate; a list selects an export
        # set.
        exports = state.proposal.get('library_exports')
        published = state.candidate
        if exports is not None:
            if not isinstance(exports, list) or any(
                (not isinstance(n, str) or n not in state.candidate for n in exports)
            ):
                raise ValueError('Invalid library export selection')
            # Publish the selected exports with their dependency closure, alongside the
            # remaining library components.
            closure = set(exports)
            while True:
                expanded = closure | {
                    d for n in closure for d in state.candidate[n]['dependencies']
                }
                if not expanded <= set(state.candidate):
                    raise ValueError('Unresolved exported dependency')
                if expanded == closure:
                    break
                closure = expanded
            # published changes from the candidate mapping to a copy of the committed library
            # here.
            published = copy.deepcopy(self.library.manifest['components'])
            published.update({n: state.candidate[n] for n in closure})
        previous = self.library.manifest['components']
        delta = {
            'changed': [n for n, c in published.items() if previous.get(n) != c],
            'removed': [n for n in previous if n not in published],
        }
        commit = self.library.commit(
            state.base_library_hash,
            published,
            evidence,
            state.proposal,
            validated_candidate=state.candidate,
        )
        state.publication_delta = delta
        state.published_commit = commit
        self.transition(Phase.DETECT)
        return commit
