"""State shared by the development stages and saved in checkpoints.

AlgorithmState groups the candidate, reuse decisions, checks, and repair data.
ExecutionState holds invocation records. CheckpointView connects these attributes
to the dictionary fields used by the adapter when it saves a run.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, TypedDict


class Phase(StrEnum):
    """Names of the controller's development and publication phases.

    Each enum value is the string stored in the checkpoint. READY means the
    candidate can be executed; DETECT starts the review after publication.

    Example::

        phase = Phase('ready')
        assert phase == Phase.READY

    Loading a saved string produces the same enum used in controller branches.
    """

    IDLE = 'idle'
    PLAN = 'plan'
    ANALYZE = 'analyze'
    PROPOSE = 'propose'
    EDIT = 'edit'
    VALIDATE = 'validate'
    REPAIR = 'repair'
    PATCH = 'patch'
    READY = 'ready'
    BLOCKED = 'blocked'
    DETECT = 'detect'


class Component(TypedDict):
    """Source and callable description of one candidate skill component.

    id names the component; language selects its executor. source holds its code,
    while hash identifies the version checked by analysis and validation. interface
    describes call arguments and results, contract records expected behavior, and
    dependencies lists the other component IDs it calls.

    Example, when preparing a task call::

        component = state.candidate[component_id]
        arguments_schema = component['interface']['args_schema']

    The executor uses that schema to check the supplied call arguments.
    """

    id: str
    language: str
    source: str
    hash: str
    interface: dict[str, Any]
    contract: dict[str, Any]
    dependencies: list[str]


class ReusePlan(TypedDict, total=False):
    """Requirements and the skills chosen to satisfy them.

    requirements is a list of requirement IDs or requirement records. choices maps
    each ID to a call, extraction, or new-component choice. Existing choices carry
    the selected component's source hash. dependencies describes how these choices
    relate to one another.

    Example::

        plan = ReusePlan(
            requirements=['build'],
            choices={'build': {'kind': 'new', 'component_id': 'machine'}},
        )

    This plan asks development to create a component for the build requirement.
    """

    requirements: list[str | dict[str, Any]]
    choices: dict[str, dict[str, Any]]
    dependencies: dict[str, Any] | list[Any]


class ComponentAnalysis(TypedDict):
    """Dependency and caller analysis tied to a component version and reuse plan.

    source_hash and plan_hash identify the inputs. context_profile records the
    analysis context, analysis_key combines those inputs for cache lookup, and
    analysis contains the operator's findings.

    Example::

        record = state.component_analyses[component_id]
        current = record['source_hash'] == state.candidate[component_id]['hash']

    current tells the next stage whether this record describes the candidate source.
    """

    source_hash: str
    analysis_key: str
    plan_hash: str
    context_profile: str | None
    analysis: dict[str, Any]


class ValidationEvidence(TypedDict, total=False):
    """Outcome of a development check for one candidate version.

    id identifies the check; status is PASS, FAIL, or PENDING. candidate_hash names
    the checked candidate, and observed contains the returned result or diagnostic.

    Example::

        passed = all(item['status'] == 'PASS' for item in state.validation_evidence)

    The controller uses these outcomes to choose execution or repair.
    """

    id: str
    candidate_hash: str
    status: Literal['PASS', 'FAIL', 'PENDING']
    observed: Any


class Proposal(TypedDict, total=False):
    """Coordinated edits, task invocation, and checks for a candidate.

    edits orders new components and rewrites, including their source versions and
    prerequisites. task_entry describes the intended call. preservation_checks test
    existing behavior; structural_checks test the proposed organization.
    library_exports selects components to publish, and blockers records work that
    still needs a decision before implementation.

    Example::

        edits = state.proposal['edits']
        targets = [edit['target'] for edit in edits]

    targets follows the implementation order selected by Propose.
    """

    id: str
    revision: int
    base_version: int
    edits: list[dict[str, Any]]
    task_entry: dict[str, Any]
    preservation_checks: list[dict[str, Any]]
    structural_checks: list[dict[str, Any]]
    library_exports: list[str]
    blockers: list[Any]


class RepairDiagnosis(TypedDict, total=False):
    """Reflect's issues and source locations for the next repair.

    G maps a component or proposal name to its issue records. Each issue supplies
    revision_scope and repair_requirements. L maps names to related source locations.

    Example::

        issues = diagnosis['G'].get(component_id, [])
        scopes = {issue['revision_scope'] for issue in issues}

    The controller uses these scopes to revisit the plan, proposal, or component.
    """
    # G maps names to issues; L maps names to the source locations used by Reflect.

    G: dict[str, list[dict[str, Any]]]
    L: dict[str, Any]


@dataclass
class ExecutionState:
    """Invocation progress and results belonging to the current task.

    invocation holds the running or completed call; last_result holds its return.
    pending_request identifies an environment operation awaiting a receipt. recovery
    and recovery_history keep interrupted invocation records. executed_candidate_hash
    and version_segments identify the component versions used during execution.
    last_operation_error records the latest failed operation. legacy_frames and
    suspended_legacy hold executions saved by the instruction-frame backend.

    When linked to a CheckpointView, assigning a field also updates its saved key.

    Example::

        state.execution.last_result = {'execution': 'completed', 'result': result}

    This stores the component return for the controller's next development decision.
    """

    invocation: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None
    pending_request: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    recovery_history: list[dict[str, Any]] = field(default_factory=list)
    executed_candidate_hash: str | None = None
    version_segments: list[dict[str, Any]] = field(default_factory=list)
    last_operation_error: dict[str, Any] | None = None
    legacy_frames: list[dict[str, Any]] = field(default_factory=list)
    suspended_legacy: dict[str, Any] | None = None

    def __setattr__(self, name, value):
        """Assign an execution field and update its linked checkpoint entry."""

        object.__setattr__(self, name, value)
        view = self.__dict__.get('_checkpoint_view')
        if view is not None and name in EXECUTION_ATTRIBUTE_FIELDS:
            dict.__setitem__(view, EXECUTION_ATTRIBUTE_FIELDS[name], value)


@dataclass
class AlgorithmState:
    """Working state of one task or maintenance operation.

    candidate is a component-ID mapping copied from the library at base_library_hash.
    reuse_plan chooses reusable and new capabilities; component_analyses stores the
    dependency findings used to assemble proposal. validation_evidence and
    repair_diagnosis carry the check results and required fixes between stages.

    edit_cursor and repair_cursor mark completed work within their respective lists.
    operator_history records operator decisions; retrieval holds the current search
    results. last_valid_plan, plan_rejection, blocked_delivery, proposal_diagnostics,
    and failure retain the information needed to continue interrupted development.

    task_binding and context_profile identify the current task context. execution
    stores its calls and results. awaiting_task_verdict and needs_replan select work
    to perform when the adapter next enters development.

    accepted_task_receipt, published_commit, and publication_delta record acceptance
    and publication. refactor_opportunity starts a maintenance goal; is_maintenance
    and task_development_archive distinguish that work from the accepted task.

    Example::

        state = AlgorithmState()
        state.candidate = candidate
        state.phase = Phase.VALIDATE

    Validate will check this candidate when the controller advances the state.
    """

    phase: Phase = Phase.IDLE
    base_library_hash: str = ''
    candidate: dict[str, Component] = field(default_factory=dict)
    reuse_plan: ReusePlan | None = None
    component_analyses: dict[str, ComponentAnalysis] = field(default_factory=dict)
    proposal: Proposal | None = None
    validation_evidence: list[ValidationEvidence] = field(default_factory=list)
    repair_diagnosis: RepairDiagnosis | None = None
    refactor_opportunity: dict[str, Any] | None = None
    operator_history: list[dict[str, Any]] = field(default_factory=list)
    retrieval: dict[str, Any] = field(default_factory=dict)
    last_valid_plan: ReusePlan | None = None
    plan_rejection: dict[str, Any] | None = None
    context_profile: str | None = None
    edit_cursor: int = 0
    repair_targets: list[str] = field(default_factory=list)
    repair_cursor: int = 0
    blocked_delivery: dict[str, Any] | None = None
    proposal_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    accepted_task_receipt: dict[str, Any] | None = None
    published_commit: str | None = None
    publication_delta: dict[str, Any] | None = None
    is_maintenance: bool = False
    task_development_archive: str | None = None
    failure: dict[str, Any] | None = None
    task_binding: dict[str, Any] | None = None
    awaiting_task_verdict: bool = False
    needs_replan: bool = False
    execution: ExecutionState = field(default_factory=ExecutionState)

    def __setattr__(self, name, value):
        """Assign a state field and write its serialized value to the checkpoint."""

        object.__setattr__(self, name, value)
        view = self.__dict__.get('_checkpoint_view')
        if view is not None:
            key = ATTRIBUTE_FIELDS.get(name)
            if key is not None:
                dict.__setitem__(view, key, value.value if name == 'phase' else value)


@dataclass(frozen=True)
class StageResult:
    """Result returned when the controller finishes a development advance.

    phase names the saved stopping point. ready_to_execute tells the adapter to
    prepare the task invocation; blocked records a component delivery awaiting work.

    Example::

        outcome = controller.advance(public)
        if outcome.ready_to_execute:
            entry = controller.state.proposal['task_entry']

    The adapter can now prepare entry for execution.
    """

    phase: Phase
    ready_to_execute: bool = False
    blocked: bool = False


# Saved field name -> AlgorithmState attribute, including nested ExecutionState fields.

WIRE_FIELDS = {
    'phase': 'phase',
    'parent': 'base_library_hash',
    'candidate': 'candidate',
    'p': 'reuse_plan',
    'A': 'component_analyses',
    'P': 'proposal',
    'E': 'validation_evidence',
    'r': 'repair_diagnosis',
    'o': 'refactor_opportunity',
    'history': 'operator_history',
    'U': 'retrieval',
    'last_valid_plan': 'last_valid_plan',
    'plan_rejection': 'plan_rejection',
    'context_profile': 'context_profile',
    'edit_cursor': 'edit_cursor',
    'repair_targets': 'repair_targets',
    'repair_cursor': 'repair_cursor',
    'blocked_delivery': 'blocked_delivery',
    'proposal_diagnostics': 'proposal_diagnostics',
    'accepted_receipt': 'accepted_task_receipt',
    'published': 'published_commit',
    'publication_delta': 'publication_delta',
    'maintenance': 'is_maintenance',
    'task_development_archive': 'task_development_archive',
    'failure': 'failure',
    'binding': 'task_binding',
    'awaiting_task_verdict': 'awaiting_task_verdict',
    'needs_replan': 'needs_replan',
    'python_execution': 'execution.invocation',
    'last_execution': 'execution.last_result',
    'pending': 'execution.pending_request',
    'execution_recovery': 'execution.recovery',
    'execution_recovery_history': 'execution.recovery_history',
    'executed_candidate_hash': 'execution.executed_candidate_hash',
    'execution_segments': 'execution.version_segments',
    'last_operation_error': 'execution.last_operation_error',
    'frames': 'execution.legacy_frames',
    'suspended_execution': 'execution.suspended_legacy',
}
ATTRIBUTE_FIELDS = {attribute: key for key, attribute in WIRE_FIELDS.items()}
EXECUTION_ATTRIBUTE_FIELDS = {
    attribute.split('.', 1)[1]: key
    for key, attribute in WIRE_FIELDS.items()
    if attribute.startswith('execution.')
}


def state_attribute(state, path):
    """Return the state object and attribute named by a checkpoint field mapping."""
    if path.startswith('execution.'):
        return state.execution, path.split('.', 1)[1]
    return state, path


class CheckpointView(dict):
    """Dictionary representation linked to an AlgorithmState.

    WIRE_FIELDS maps saved names such as p, P, A, and E to their state attributes.
    Writing through either the view or an attribute updates both representations.
    Deleting a mapped key restores that attribute's dataclass default. Additional
    adapter fields stay in the dictionary and are included when it is serialized.

    Example::

        state = AlgorithmState()
        view = CheckpointView(state, {'phase': 'plan'})
        state.phase = Phase.READY
        assert view['phase'] == 'ready'

    The in-memory enum becomes its string value in the saved dictionary.
    """

    def __init__(self, state: AlgorithmState, document=None):
        """Load dictionary fields and attach the view to both state objects."""

        super().__init__(document or {})
        self.state = state
        for key, value in (document or {}).items():
            if key in WIRE_FIELDS:
                owner, attribute = state_attribute(state, WIRE_FIELDS[key])
                # The saved phase is a str; the state attribute is converted to Phase.
                setattr(owner, attribute, Phase(value) if key == 'phase' else value)
        object.__setattr__(state, '_checkpoint_view', self)
        object.__setattr__(state.execution, '_checkpoint_view', self)

    def __setitem__(self, key, value):
        """Write a saved field and assign its corresponding state attribute."""

        dict.__setitem__(self, key, value)
        if key in WIRE_FIELDS:
            owner, attribute = state_attribute(self.state, WIRE_FIELDS[key])
            # The saved phase is a str; the state attribute is converted to Phase.
            setattr(owner, attribute, Phase(value) if key == 'phase' else value)

    def __delitem__(self, key):
        """Remove a saved field and restore the mapped attribute default."""

        dict.__delitem__(self, key)
        if key in WIRE_FIELDS:
            owner, attribute = state_attribute(self.state, WIRE_FIELDS[key])
            default_owner, default_attribute = state_attribute(
                AlgorithmState(), WIRE_FIELDS[key]
            )
            object.__setattr__(owner, attribute, getattr(default_owner, default_attribute))

    def pop(self, key, *default):
        """Return and delete a field, applying the same state update as deletion."""

        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def setdefault(self, key, default=None):
        """Initialize a missing field through the linked dictionary assignment."""

        if key not in self:
            self[key] = default
        return self[key]

    def update(self, other=(), **values):
        """Assign each supplied field through the linked dictionary assignment."""

        for key, value in dict(other, **values).items():
            self[key] = value

    def to_document(self):
        """Return a plain dictionary for checkpoint serialization."""

        return dict(self)

    def __deepcopy__(self, memo):
        """Copy checkpoint values into a plain dictionary, leaving the live view attached."""
        import copy

        return copy.deepcopy(dict(self), memo)
