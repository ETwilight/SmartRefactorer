"""Algorithm state and the explicit codec for historical checkpoints.

Readable internal names are independent of the persisted p/P/A/E/r/o wire keys.
Only the controller changes phase; stages update the data they are responsible
for and return control to that controller. Host/model session fields retained
from older files are compatibility data, not additional algorithm phases.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, TypedDict


class Phase(StrEnum):
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
    id: str
    language: str
    source: str
    hash: str
    interface: dict[str, Any]
    contract: dict[str, Any]
    dependencies: list[str]


class ReusePlan(TypedDict, total=False):
    requirements: list[str | dict[str, Any]]
    choices: dict[str, dict[str, Any]]
    dependencies: dict[str, Any] | list[Any]


class ComponentAnalysis(TypedDict):
    source_hash: str
    analysis_key: str
    plan_hash: str
    context_profile: str | None
    analysis: dict[str, Any]


class ValidationEvidence(TypedDict, total=False):
    id: str
    candidate_hash: str
    status: Literal['PASS', 'FAIL', 'PENDING']
    observed: Any


class Proposal(TypedDict, total=False):
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
    # G/L remain model-output field names, not ambiguous state variable names.
    G: dict[str, list[dict[str, Any]]]
    L: dict[str, Any]


@dataclass
class ExecutionState:
    """Execution facts, separate from development phases and task acceptance.

    Legacy frames are historical evidence only. A normal Python invocation has
    no claimed serializable stack; recovery contains observations and receipts.
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
        object.__setattr__(self, name, value)
        view = self.__dict__.get('_checkpoint_view')
        if view is not None and name in EXECUTION_ATTRIBUTE_FIELDS:
            dict.__setitem__(view, EXECUTION_ATTRIBUTE_FIELDS[name], value)


@dataclass
class AlgorithmState:
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
        object.__setattr__(self, name, value)
        view = self.__dict__.get('_checkpoint_view')
        if view is not None:
            key = ATTRIBUTE_FIELDS.get(name)
            if key is not None:
                dict.__setitem__(view, key, value.value if name == 'phase' else value)


@dataclass(frozen=True)
class StageResult:
    phase: Phase
    ready_to_execute: bool = False
    blocked: bool = False


# Explicit migration mapping: renaming a Python attribute does not rewrite the
# old checkpoint or invalidate its finalized report hash.
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
    """Resolve an explicitly mapped field; never interpret arbitrary JSON paths."""
    if path.startswith('execution.'):
        return state.execution, path.split('.', 1)[1]
    return state, path


class CheckpointView(dict):
    """Historical serialization/API compatibility; new stages use typed attributes.

    Preserve key presence and unknown host fields exactly. This view exists for
    old callers and checkpoint writers while adapters migrate their own state;
    it is not a second controller and contains no transition rules.
    """

    def __init__(self, state: AlgorithmState, document=None):
        super().__init__(document or {})
        self.state = state
        for key, value in (document or {}).items():
            if key in WIRE_FIELDS:
                owner, attribute = state_attribute(state, WIRE_FIELDS[key])
                setattr(owner, attribute, Phase(value) if key == 'phase' else value)
        object.__setattr__(state, '_checkpoint_view', self)
        object.__setattr__(state.execution, '_checkpoint_view', self)

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if key in WIRE_FIELDS:
            owner, attribute = state_attribute(self.state, WIRE_FIELDS[key])
            setattr(owner, attribute, Phase(value) if key == 'phase' else value)

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        if key in WIRE_FIELDS:
            owner, attribute = state_attribute(self.state, WIRE_FIELDS[key])
            default_owner, default_attribute = state_attribute(
                AlgorithmState(), WIRE_FIELDS[key]
            )
            object.__setattr__(owner, attribute, getattr(default_owner, default_attribute))

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, other=(), **values):
        for key, value in dict(other, **values).items():
            self[key] = value

    def to_document(self):
        return dict(self)

    def __deepcopy__(self, memo):
        """A checkpoint snapshot is plain wire data, not a second live state view."""
        import copy

        return copy.deepcopy(dict(self), memo)
