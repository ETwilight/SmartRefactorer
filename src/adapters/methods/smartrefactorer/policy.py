"""Connect the SR controller to model calls, evidence storage, and run callbacks.

bind() loads the method checkpoint and creates the library, controller, model
client, embeddings, execution bridge, and host coordinator for the current run.
"""

import copy
import hashlib
import json
import shutil
from pathlib import Path
from algorithms.smartrefactorer.controller import Controller
from algorithms.smartrefactorer.library import Library, atomic, digest
from algorithms.smartrefactorer.execution.bindings import validate_template, BindingError
from algorithms.smartrefactorer.execution.python_backend import PROFILE
from algorithms.smartrefactorer.state import Phase
from .coordinator import HostCoordinator, GUIDANCE, DECISION_SCHEMA, CONTEXT_PROFILE
from .embeddings import DenseEmbeddings
from .evidence import EvidenceCatalog
from .execution import ExecutionBridge
from .model import OperatorClient


class MethodAdapter:
    """Supply the SR method interface used by the task runner.

    resume is an optional source-run Path. environment supplies public requests and
    knowledge reads. bind(folder, recorder) creates store under method/state and
    loads the saved development dictionary before constructing the Controller.

    controller holds algorithm state; coordinator selects the next run operation.
    model handles operator calls, embeddings provides the retrieval vectors, and
    evidence stores original feedback. execution exchanges live skill requests and
    their receipts. These objects share the recorder and persistence callback.

    The runner calls decide(), passes requests through request_stream(), and sends
    completed feedback to on_feedback(). persist() writes the linked checkpoint.

    Example, after the runner has allocated the environment and recorder::

        adapter = MethodAdapter(environment=environment)
        adapter.bind(folder, recorder)
        decision = adapter.decide(context)

    decision selects a public request, development, or execution for this task.
    """

    compact_transport = False
    programmatic_continuation = True
    records_only_on_round_end = True
    instructions = GUIDANCE
    decision_schema = DECISION_SCHEMA

    def __init__(
        self,
        resume=None,
        environment=None,
        *,
        embedding_device='cpu',
        embedding_precision='float32'
    ):
        """Keep the continuation source, environment, and embedding configuration."""

        self.resume = Path(resume) if resume else None
        self.environment = environment
        self.memory = ''
        self.embedding_device = embedding_device
        self.embedding_precision = embedding_precision

    @property
    def environment(self):
        """Return the environment currently used by this method."""

        return self._environment

    @environment.setter
    def environment(self, value):
        """Update the environment used by the model client and execution bridge."""

        self._environment = value
        if hasattr(self, 'model'):
            self.model.environment = value
        if hasattr(self, 'execution'):
            self.execution.environment = value

    def bind(self, folder, recorder):
        """Load saved method state and construct the objects used for this run."""

        self.folder = Path(folder) / 'method/state'
        self.recorder = recorder
        self.store = self.folder / 'smartrefactorer'
        if self.resume:
            shutil.copytree(
                self.resume / 'method/state/smartrefactorer', self.store, dirs_exist_ok=True
            )
        self.library = Library(self.store / 'library')
        path = self.store / 'development.json'
        # saved: dict | None, the serialized development state passed to the Controller.
        saved = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else None
        self.evidence = EvidenceCatalog(self.store, recorder)
        self.embeddings = DenseEmbeddings(
            self.store,
            recorder,
            self.guard,
            device=self.embedding_device,
            precision=self.embedding_precision,
        )
        self.model = OperatorClient(recorder, self.environment, self.evidence, self.persist)
        self.controller = Controller(
            self.library,
            self.model.ask,
            self.embeddings.embed,
            saved,
            run_fixture=self.run_fixture,
            save=self.persist,
            archive_accepted_state=self.archive_accepted_state,
        )
        # The model client's controller changes from None to this run's Controller.
        self.model.controller = self.controller
        self.evidence.controller = self.controller
        self.execution = ExecutionBridge(
            self.controller, recorder, self.environment, self.store, self.persist, self.guard
        )
        self.coordinator = HostCoordinator(self)
        self._restore_adapter_state(saved is not None)
        self.evidence.sync_evidence()
        self.persist()

    def _restore_adapter_state(self, had_checkpoint):
        """Restore saved invocation facts, context settings, and pending request feedback."""
        # saved: CheckpointView, linked to AlgorithmState; writes here update the same live
        # state.
        saved = self.controller.s
        if had_checkpoint and (
            saved.get('execution_profile') != PROFILE
            or saved.get('operator_record_protocol') != 'executable-checks-v1'
        ):
            session = saved.get('operator_session')
            if session:
                saved['operator_format_migration'] = {
                    'session_key': session['key'],
                    'role': session['role'],
                    'phase': saved['phase'],
                    'edit_cursor': saved.get('edit_cursor'),
                    'repair_cursor': saved.get('repair_cursor'),
                    'candidate_hash': digest(saved['candidate']),
                    'binding': copy.deepcopy(saved.get('binding')),
                }
        saved['execution_profile'] = PROFILE
        saved['operator_record_protocol'] = 'executable-checks-v1'
        execution = self.controller.state.execution
        if execution.invocation and execution.invocation.get('status') in (
            'running',
            'waiting',
            'interrupted',
            'failed',
        ):
            if execution.invocation['status'] in ('running', 'waiting'):
                execution.invocation['status'] = 'interrupted'
            execution.recovery = copy.deepcopy(execution.invocation)
            execution.recovery['pending_request'] = copy.deepcopy(execution.pending_request)
        if (
            had_checkpoint
            and (not execution.legacy_frames)
            and (not execution.pending_request)
            and (not self.controller.state.awaiting_task_verdict)
            and (self.controller.state.phase in (Phase.EDIT, Phase.VALIDATE, Phase.READY))
        ):
            entry = (self.controller.state.proposal or {}).get('task_entry')
            if entry:
                try:
                    validate_template(entry.get('argument_bindings', {}))
                except BindingError as error:
                    self.controller.record_invocation_issue(error)
        if self.resume and self.controller.state.phase == Phase.BLOCKED:
            self.controller.transition(self.controller.state.blocked_delivery['resume_phase'])
        failure = self.controller.state.failure
        if failure and failure.get('type') == 'ModelOutputError':
            saved['output_failure'] = failure
            self.controller.state.failure = None
        if had_checkpoint and self.controller.state.context_profile != CONTEXT_PROFILE:
            self.controller.state.needs_replan = True
            self.controller.state.operator_history.append(
                {
                    'kind': 'context_invalidation',
                    'reason': 'Host admission, receipt recovery, budget and persistence are supplied capabilities, not replacement skill goals. Replan actual task capabilities while retaining prior valid requirements, candidate code and exact history. Pending downstream work must not discard applicable candidate edits.',
                }
            )
        history = self.controller.state.operator_history
        last_invalidation = max(
            (
                index
                for index, item in enumerate(history)
                if item.get('kind') == 'context_invalidation'
            ),
            default=-1,
        )
        last_plan = max(
            (index for index, item in enumerate(history) if item.get('role') == 'Plan'),
            default=-1,
        )
        if last_invalidation > last_plan:
            self.controller.state.needs_replan = True
        self.controller.state.context_profile = CONTEXT_PROFILE
        pending = execution.pending_request
        if pending:
            matches = [
                call
                for call in self.recorder.env_calls
                if call['request'] == pending['request']
            ]
            if matches:
                self.execution.on_feedback(pending['request'], matches[-1]['feedback'])
            elif (
                self.resume
                and getattr(self.recorder, 'verified_unstarted_method_request', None)
                == pending['request']
            ):
                if pending.get('program_invocation_id'):
                    saved['retired_unsent_request'] = {
                        'request': copy.deepcopy(pending['request']),
                        'execution': 'not_started',
                        'evidence': 'Verified complete source ledger contains no dispatch start',
                    }
                    execution.pending_request = None
                else:
                    pending['dispatch_permitted'] = True
                self.recorder.verified_unstarted_method_request = None

    def persist(self):
        """Write the current controller checkpoint to development.json."""

        if hasattr(self, 'controller'):
            atomic(self.store / 'development.json', self.controller.s)

    def guard(self):
        """Call the recorder's configured run-limit check."""

        if self.recorder.model_guard:
            self.recorder.model_guard()

    def archive_accepted_state(self):
        """Save the accepted task development state and return its file path."""

        path = self.store / 'accepted-task-development.json'
        atomic(path, self.controller.s)
        return str(path)

    def public(self, context):
        """Collect the task, environment, limits, and recent feedback supplied to
        development.
        """

        return {
            'goal': context['goal'],
            'task_binding': copy.deepcopy(self.recorder.active_task),
            'active_task': context.get('active_task'),
            'discovery': context.get('discovery'),
            'task_workspace': context.get('task_workspace'),
            'limits': context.get('limits'),
            'history': context['history'][-6:],
            'history_access': 'Older original feedback remains available via feedback.list/get.',
        }

    def decide(self, context):
        """Ask the host coordinator for the next run decision."""

        return self.coordinator.decide(context)

    def maintenance(self, context):
        """Continue the current library-maintenance goal."""

        return self.coordinator.maintain(context)

    def request_stream(self, requests, context):
        """Convert the selected requests into the next public request stream."""

        return self.execution.request_stream(requests, context)

    def on_feedback(self, request, feedback):
        """Pass a request and its returned receipt to the execution bridge."""

        return self.execution.on_feedback(request, feedback)

    def on_round_end(self, record):
        """Record a live invocation's interruption and persist its state."""

        return self.execution.on_round_end(record)

    def ask(self, role, instructions, data):
        """Request a parsed operator record from the model client."""

        return self.model.ask(role, instructions, data)

    def embed(self, texts):
        """Return the retrieval vectors for the supplied texts."""

        return self.embeddings.embed(texts)

    def run_fixture(self, components, component_id, arguments, receipts=()):
        """Run candidate code with the supplied recorded request feedback."""

        return self.execution.run_fixture(components, component_id, arguments, receipts)

    def start_program(self, *args, **kwargs):
        """Start a candidate invocation through the execution bridge."""

        return self.execution.start_program(*args, **kwargs)

    def program_stream(self):
        """Read the live invocation until its next request or final event."""

        return self.execution.program_stream()

    def sync_evidence(self):
        """Update the feedback and development-record indexes."""

        return self.evidence.sync_evidence()

    @property
    def dev(self):
        """Return the algorithm Controller."""
        return self.controller

    @property
    def program(self):
        """Return the Program attached to this run, or None."""

        return self.execution.program

    @property
    def requests(self):
        """Return the model-call metadata recorded by the operator client."""

        return self.model.requests

    @property
    def encoder(self):
        """Return the loaded embedding encoder, or None before its first use."""

        return self.embeddings.encoder

    @encoder.setter
    def encoder(self, value):
        """Set the embedding encoder used by retrieval."""

        self.embeddings.encoder = value

    @property
    def evidence_store(self):
        """Return the content store containing original evidence records."""

        return self.evidence.contexts

    @property
    def evidence_index(self):
        """Return the evidence-ID mapping used for request lookup."""

        return self.evidence.index

    def summary(self):
        """Persist state and return its checkpoint, library, and embedding-cache identifiers."""
        self.persist()
        return {
            'method': 'SmartRefactorer',
            'language': 'python-v1',
            'library_version': self.library.manifest['version'],
            'checkpoint_sha256': hashlib.sha256(
                (self.store / 'development.json').read_bytes()
            ).hexdigest(),
            'embedding_cache_sha256': (
                hashlib.sha256((self.store / 'embedding-cache.json').read_bytes()).hexdigest()
                if (self.store / 'embedding-cache.json').exists()
                else None
            ),
            'library_manifest_hash': digest(self.library.manifest),
            'implementation_revision': 'refactor-v1',
            'components': len(self.library.manifest['components']),
            'phase': self.dev.s['phase'],
            'accepted_receipt': self.dev.s.get('accepted_receipt'),
            'implementation_status': 'Normal Python backend; offline behavioral validation; native task acceptance not verified',
        }

    @property
    def feedback(self):
        """Return the execution feedback collected for the current decision."""

        return self.execution.feedback
