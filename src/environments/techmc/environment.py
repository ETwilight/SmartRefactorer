"""TechMC 公共请求入口：发现能力、交付任务、执行动作和接收验收结果。

handle 负责请求身份与回执账本，_dispatch 按操作分发，_primitive 连接执行后端。
任务定义来自 task_catalog，机器验收由 evaluation 与 acceptance 规则计算。
"""

import copy
import json
import time
import threading
import uuid
import re
import math
from urllib.parse import urlsplit
import importlib


def _module(name):
    return importlib.import_module((__package__ + '.' if __package__ else '') + name)


_contracts = _module('contracts')
TaskSelection, managed_selection = [
    getattr(_module('task_selection'), k) for k in ('TaskSelection', 'managed_selection')
]
ROOT, VERSION, ACTION, SUBMISSION, SCHEMAS, POS, VEC, obj, validate = [
    getattr(_contracts, k)
    for k in (
        'ROOT',
        'VERSION',
        'ACTION',
        'SUBMISSION',
        'SCHEMAS',
        'POS',
        'VEC',
        'obj',
        'validate',
    )
]
_action_catalog = _module('actions.catalog')
TOOLS = _action_catalog.TOOLS
KNOWLEDGE_METHODS = _action_catalog.KNOWLEDGE_METHODS
LOCAL_DIAGNOSTICS = _action_catalog.LOCAL_DIAGNOSTICS
DIRECT_DIAGNOSTICS = LOCAL_DIAGNOSTICS | {'observe.performance'}
TASK_COMPLETION_GUIDE = '''The published task stream identifies the current task visit. Work on that task;
the experiment host delivers later visits. Each task has its own world: its first
visit starts fresh and later visits resume that same task world. One task instance
and product profile are active within a visit. Earlier task feedback remains readable.
Whenever a valid independent evaluation passes a task, its completion is recorded,
including after earlier unsuccessful attempts or deferral. Every evaluation is
retained; a later failure does not revoke an earlier pass. Repeated passes do not
add another distinct completed task. The learning algorithm may compare its own
earlier and later evaluations to identify regression; the environment does not grade regression.
Task status can be recorded with task.progress params={task_id, state, reason}:
state=deferred records temporary deferral; state=active records resumption;
state=abandoned ends the current attempt. These choices are not evaluation verdicts
and do not prevent a later researcher-scheduled visit to the same task.
For legacy catalogue consumers without a task stream, the learner chooses task
order and return timing; completion need not follow catalogue or start order.
A task round binds one task instance until a new valid passing evaluation or an
explicit task status boundary. Failed, inconclusive or errored evaluations leave
that task round open for further work. Run interruption preserves a partial round.
'''
Bridge, BridgeError = _module('adapters.bridge').Bridge, _module('adapters.bridge').BridgeError
OperationNotStarted = _module('adapters.bridge').OperationNotStarted
ActionSession, atomic_json = (
    _module('runtime_support').ActionSession,
    _module('runtime_support').atomic_json,
)
LocalExecutionError = _module('runtime_support').LocalExecutionError
list_tasks, get_task, stable_hash, select_profile = [
    getattr(_module('task_catalog'), k)
    for k in ('list_tasks', 'get_task', 'stable_hash', 'select_profile')
]
Evaluator, validate_submission = (
    _module('evaluation').Evaluator,
    _module('evaluation').validate_submission,
)
preflight_submission = _module('evaluation').preflight_submission
public_result = _module('feedback').public_result
CliBridge, ExecutionAgent = _module('executors').CliBridge, _module('executors').ExecutionAgent
ExecutionAgentError = _module('executors').ExecutionAgentError

from .actions.validation import parameter_schema, action_catalog, check_action


class Environment:
    def task_index(self, feedback):
        return _module('guidance').task_index(feedback)

    @staticmethod
    def _validated(function, *args, **kwargs):
        """Only explicit pure preparation steps can establish not-started."""
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            raise OperationNotStarted(exc) from exc

    def __init__(
        self,
        actor='ETwilight',
        run_id=None,
        session=None,
        allow_execution_agent=False,
        max_evaluation_volume=262144,
        max_evaluation_chunks=64,
        server_port=None,
        client_port=None,
        task_selection=None,
        resume_existing=False,
    ):
        selection_source = 'explicit'
        selected_port = server_port
        if selected_port is None and session is not None:
            url = getattr(getattr(session, 'server', None), 'url', None)
            selected_port = urlsplit(url).port if isinstance(url, str) else None
        elif selected_port is None:
            import os

            selected_port = int(os.environ.get('MC_AGENT_SERVER_PORT', '8776'))
        managed_spec, managed_source = managed_selection(ROOT, selected_port)
        if task_selection is None:
            task_selection, selection_source = managed_spec, managed_source
        world_type = managed_spec.get('world_type') if isinstance(managed_spec, dict) else None
        self.task_selection = TaskSelection(task_selection, world_type=world_type)
        self.task_selection_source = selection_source
        self.session = session or ActionSession(
            actor,
            run_id,
            client_required=False,
            server_port=server_port,
            client_port=client_port,
            resume_existing=resume_existing,
        )
        self.actor = actor
        self.used_game = False
        self.closed = False
        self.allow_execution_agent = allow_execution_agent
        self.max_evaluation_volume = max_evaluation_volume
        self.max_evaluation_chunks = max_evaluation_chunks
        self.ledger_path = self.session.directory / 'api-ledger.json'
        self.ledger = {}
        self.evaluating = False
        self._ledger_lock = threading.RLock()
        self._mutation_lock = threading.RLock()
        self.task_stream = None
        self.root = ROOT
        self._stream_instances = set()
        self.task_progress = {}
        self.machines = []
        self.infrastructure_events = []
        self.task_worlds = None
        self._active_task_instance = None
        self._active_stream_position = None
        self.task_activity = {}
        self._inflight = 0
        self._evaluation_children = []
        state_path = self.session.directory / 'environment-state.json'
        self._restored_state = state_path.exists()
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if (
                state.get('actor') != self.actor
                or state.get('server_bridge') != self.session.server.url
            ):
                raise ValueError('Persisted environment identity does not match this session')
            self.task_progress = state.get('task_progress', {})
            self.machines = state.get('machines', [])
            self.task_activity = state.get('task_activity', {})
            self._stream_instances = set(state.get('stream_instances', []))
            self._active_task_instance = state.get('active_task_instance')
            self._active_stream_position = state.get('active_stream_position')
        if self.ledger_path.exists():
            self.ledger = json.loads(self.ledger_path.read_text(encoding='utf-8'))

    def _persist_state(self, **updates):
        state = {
            'schema_version': 1,
            'actor': self.actor,
            'server_bridge': self.session.server.url,
            'task_progress': self.task_progress,
            'task_activity': self.task_activity,
            'machines': self.machines,
            'stream_instances': sorted(self._stream_instances),
            'active_task_instance': self._active_task_instance,
            'active_stream_position': self._active_stream_position,
        }
        state.update(updates)
        atomic_json(self.session.directory / 'environment-state.json', state)

    def _save_ledger(self):
        with self._ledger_lock:
            atomic_json(self.ledger_path, self.ledger)

    def enable_task_worlds(self, **configuration):
        """Install trusted per-task world switching before the first request."""
        if self.task_worlds is not None or (self.ledger and not self._restored_state):
            raise ValueError('Task workspaces must be configured before discovery')
        self.task_worlds = _module('task_worlds').TaskWorldManager(self, **configuration)
        return self.task_worlds

    def task_workspace_status(self):
        return (
            self.task_worlds.status()
            if self.task_worlds is not None
            else {'status': 'disabled'}
        )

    def resume_task_binding(self, binding):
        """Trusted checkpoint restoration; retain the original task instance.

        This method is intentionally absent from the agent RPC dispatcher. It
        never repeats a game command or creates a replacement task instance.
        """
        with self._ledger_lock:
            if (
                not binding.get('verified')
                or not self._restored_state
                or self.evaluating
                or self._inflight
            ):
                raise ValueError('Task binding resume requires a verified idle saved session')
            if self.task_stream is None or self.task_worlds is None:
                raise ValueError('Restore task stream and world manager first')
            if (
                self.task_stream.position,
                self.task_stream.current_id,
                self.session.run_id,
            ) != (binding['stream_position'], binding['task_id'], binding['session_id']):
                raise ValueError('Resumed task stream/session differs from checkpoint')
            instance_id = binding['task_instance_id']
            if instance_id not in self._stream_instances:
                raise ValueError('Resumed task instance is not owned by this session')
            instance = json.loads(
                (self.root / 'runs/task-instances' / f'{instance_id}.json').read_text(
                    encoding='utf-8'
                )
            )
            if (
                instance.get('task', {}).get('id'),
                instance.get('task', {}).get('selected_profile'),
                instance.get('stream_position'),
                instance.get('server_instance_id'),
                instance.get('workspace_world_key'),
            ) != (
                binding['task_id'],
                binding.get('profile_id'),
                binding['stream_position'],
                binding['server_instance_id'],
                binding['workspace_world_key'],
            ):
                raise ValueError('Resumed task/profile/world binding differs from checkpoint')
            if (
                instance.get('actor') != self.actor
                or instance.get('server_bridge') != self.session.server.url
            ):
                raise ValueError('Resumed task actor/server differs from session')
            if instance['task'] != select_profile(
                self.task_selection.get(binding['task_id']), binding.get('profile_id')
            ):
                raise ValueError('Resumed task definition changed')
            physical = _module('server_recovery').binding(self)
            if (
                physical.get('status') != 'ready'
                or physical.get('instance_id') != binding['server_instance_id']
                or physical.get('world') != instance.get('workspace_world')
            ):
                raise ValueError('Original physical task world is not ready')
            workspace = self.task_worlds.activate(binding['task_id'])
            if (workspace.get('world_key'), workspace.get('instance_id')) != (
                binding['workspace_world_key'],
                binding['server_instance_id'],
            ):
                raise ValueError('Trusted world activation changed checkpoint identity')
            self._persist_state(
                active_task_instance=instance_id,
                active_stream_position=binding['stream_position'],
            )
            self._active_task_instance = instance_id
            self._active_stream_position = binding['stream_position']
            self.continuation = {
                'binding': copy.deepcopy(binding),
                'source': 'explicit_saved_checkpoint',
                'handoff': 'World and session reconnected. Continue the original unfinished round; completed historical requests must not be replayed.',
            }
            return copy.deepcopy(self.continuation)

    def release_task_workspace(self, outcome):
        """Trusted host boundary: preserve the old world before releasing binding."""
        with self._ledger_lock:
            if self.evaluating or self._inflight:
                raise ValueError(
                    'Cannot release a workspace during an in-flight environment request'
                )
            self._evaluation_children = [
                child
                for child in self._evaluation_children
                if getattr(child, '_heartbeat_thread', None) is not None
                and child._heartbeat_thread.is_alive()
            ]
            if self._evaluation_children:
                raise RuntimeError(
                    'An old evaluation heartbeat has not stopped; retain its task workspace'
                )
            active = self.task_workspace_status().get('active') or {}
            for entry in self.ledger.values():
                response = entry.get('response', {})
                operation = entry.get('request', {}).get('op')
                unfinished = response.get('status') in (
                    'pending_or_unknown',
                    'running',
                ) or response.get('execution', {}).get('state') in ('unknown', 'running')
                if (
                    unfinished
                    and operation in ('action.execute', 'evaluation.run')
                    and self._mutation_request(entry.get('request', {}))
                ):
                    if not active or entry.get('workspace_world_key') == active.get(
                        'world_key'
                    ):
                        raise ValueError(
                            'Cannot release a workspace with an unresolved mutation; inspect or recover its request first'
                        )
            receipt = {'status': 'released', 'outcome': outcome}
            if (
                self.task_worlds is not None
                and self.task_worlds.status().get('active_task_id') is not None
            ):
                # Cleanup must run while the old workspace is still attached.
                if self.used_game:
                    errors = self.session.cleanup()
                    if errors:
                        raise RuntimeError('Workspace cleanup failed: ' + str(errors))
                    heartbeat = getattr(self.session, '_heartbeat_thread', None)
                    if heartbeat is not None and heartbeat.is_alive():
                        raise RuntimeError(
                            'Old workspace heartbeat has not stopped; retain the current workspace until it exits'
                        )
                    for name, value in (
                        ('created_fakes', set()),
                        ('undo', []),
                        ('client_settings', {}),
                        ('initial_clock', None),
                        ('clock_changed', False),
                        ('keep_conditions', []),
                        ('_heartbeat_stop', threading.Event()),
                        ('_heartbeat_thread', None),
                    ):
                        if hasattr(self.session, name):
                            setattr(self.session, name, value)
                    self.used_game = False
            if self.task_worlds is not None:
                receipt = self.task_worlds.release(outcome)
            self._persist_state(active_task_instance=None, active_stream_position=None)
            self._active_task_instance = None
            self._active_stream_position = None
            return receipt

    def enable_task_stream(self, **configuration):
        """Trusted experiment setup, before discovery; never exposed as an RPC."""
        if self.task_stream is not None:
            raise ValueError('Task delivery must be configured once before discovery')
        if self.ledger and not (self.session.directory / 'task-stream.json').exists():
            raise ValueError('Task delivery must be configured before discovery')
        configuration.setdefault('state_path', self.session.directory / 'task-stream.json')
        self.task_stream = _module('task_stream').TaskStream(
            self.task_selection, **configuration
        )
        return self.task_stream

    def _visible_tasks(self, difficulty=None, world_type=None):
        return (
            self.task_stream.visible(difficulty, world_type)
            if self.task_stream
            else self.task_selection.tasks(difficulty, world_type)
        )

    def _visible_task(self, task_id):
        return (
            self.task_stream.get(task_id)
            if self.task_stream
            else self.task_selection.get(task_id)
        )

    def _primitive(self, action, request_id):
        method = action['method']
        params = action['params']
        backend = action.get('executor', 'direct')
        if self.evaluating and method not in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS:
            raise ValueError(
                'Acceptance window is exclusive; only knowledge queries are available'
            )
        check_action(action)
        if self.task_stream and method not in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS:
            if self._active_task_instance is None:
                raise OperationNotStarted(
                    ValueError('Start the current task before accessing its workspace')
                )
            self._validated(self._instance, self._active_task_instance)
        # Knowledge and lightweight diagnostics never need an executor model,
        # even when the caller applies a global game-executor preference.
        if method in KNOWLEDGE_METHODS | DIRECT_DIAGNOSTICS:
            backend = 'direct'
        if method == 'experiment.resources':
            return _module('experiment_resources').read(self)
        if backend == 'execution_agent':
            if not self.allow_execution_agent:
                raise OperationNotStarted(
                    ValueError(
                        'AI execution is opt-in; start the environment with --allow-execution-agent'
                    )
                )
            requested = {**action, 'executor': 'direct'}
            return ExecutionAgent().execute(
                requested,
                lambda a: self._primitive(
                    {**a, 'executor': 'direct'},
                    request_id if a == requested else str(uuid.uuid4()),
                ),
                request_identity={
                    'method': method,
                    'params': copy.deepcopy(params),
                    'run_id': self.session.run_id,
                    'actor_id': self.actor,
                    'request_id': request_id,
                },
            )
        if action['side'] == 'local' and method.startswith('trace.'):
            Trace = _module('adapters.trace').Trace
            return getattr(Trace(self.session.run_id), method.split('.')[1])(**params)
        if action['side'] != 'local':
            self.used_game = True
            self.session.start_heartbeat()
        if action['side'] == 'client' and method.startswith('action.'):
            self.session.client_required = True
        # Keep the public ledger request while ActionSession records the real
        # native wire params. The CLI boundary uses the inverse validation view.
        step = {
            k: v
            for k, v in _module('actions.parameters').native_action(action).items()
            if k != 'executor'
        }
        step['request_id'] = request_id
        if backend == 'cli' and action['side'] != 'local':
            key = 'server' if action['side'] == 'server' else 'client'
            original = getattr(self.session, key)
            setattr(self.session, key, CliBridge(original))
            try:
                return self.session.invoke(step)
            finally:
                setattr(self.session, key, original)
        return self.session.invoke(step)

    def _instance(self, instance_id):
        if not isinstance(instance_id, str) or not all(
            c.isalnum() or c in '-_' for c in instance_id
        ):
            raise ValueError('Invalid task instance ID')
        if self.task_stream and instance_id not in self._stream_instances:
            raise ValueError('Task instance does not belong to this experiment')
        value = json.loads(
            (ROOT / 'runs/task-instances' / f'{instance_id}.json').read_text(encoding='utf-8')
        )
        self.task_selection.require(value['task']['id'])
        if self.task_stream:
            self.task_stream.require(value['task']['id'], current=True)
        if self.task_stream and (
            self._active_task_instance != instance_id
            or self._active_stream_position != self.task_stream.position
        ):
            raise ValueError('Task instance is not the active binding for this visit')
        if self.task_worlds is not None:
            active = self.task_worlds.status().get('active') or {}
            if value.get('workspace_task_id') != active.get('task_id') or value.get(
                'workspace_world_key'
            ) != active.get('world_key'):
                raise ValueError('Task instance belongs to another task workspace')
        if value['actor'] != self.actor or value['server_bridge'] != self.session.server.url:
            raise ValueError('Task instance actor/server does not match this environment')
        current = get_task(value['task']['id'])
        if 'client_actor_policy' in value['task']:
            raise ValueError(
                'Task instance uses the retired client actor policy; start a new task instance'
            )
        current_binding = _module('server_recovery').binding(self)
        if (
            current_binding
            and value.get('server_instance_id') != current_binding['instance_id']
        ):
            raise ValueError(
                'Task instance belongs to another world generation; start unfinished task again'
            )
        return value

    def _dispatch(self, op, params, request_id):
        """按 op 执行能力发现、任务交付、动作调用或机器验收。

        分支负责各操作的业务状态，公共请求身份与回执由 _handle 维护。
        """
        if op == 'environment.document':
            return _module('discovery_views').document(self, params, request_id)
        if op == 'environment.status':
            value = _module('discovery_views').status(self)
            workspace = self.task_workspace_status()
            active = workspace.get('active')
            value['task_workspace'] = {
                'enabled': self.task_worlds is not None,
                'active': (
                    {
                        k: copy.deepcopy(active[k])
                        for k in ('task_id', 'world_key', 'instance_id', 'activation_id')
                        if k in active
                    }
                    if active
                    else None
                ),
                'task_instance_id': self._active_task_instance,
                'stream_position': self._active_stream_position,
                'transition_pending': workspace.get('pending') is not None,
            }
            return value
        if op == 'registry.get':
            return _module('region_views').registry_get(self, params, request_id)
        if op == 'actions.get':
            selected = next(
                (t for t in action_catalog() if t['method'] == params['method']), None
            )
            if selected is None:
                raise OperationNotStarted(ValueError('Unknown action: ' + params['method']))
            return selected
        if op == 'schemas.get':
            if 'operation' in params:
                return self._validated(
                    _module('discovery_views').operation_schema,
                    params['name'],
                    params['operation'],
                )
            if params['name'] not in SCHEMAS:
                raise OperationNotStarted(KeyError(params['name']))
            return SCHEMAS[params['name']]
        if op == 'feedback.get':
            if params['request_id'] not in self.ledger:
                raise OperationNotStarted(KeyError(params['request_id']))
            value = _module('failure_contract').response(
                self.ledger[params['request_id']]['response']
            )
            if params.get('fields'):
                return _module('response_views').selected(value, params['fields'])
            if 'path' in params:
                for key in params['path'].split('.'):
                    value = value[int(key)] if isinstance(value, list) else value[key]
                if isinstance(value, list):
                    return {
                        'items': value[
                            params.get('offset', 0) : params.get('offset', 0)
                            + params.get('limit', 32)
                        ],
                        'total': len(value),
                    }
            return value
        if op == 'feedback.list':
            entries = []
            for key, v in self.ledger.items():
                if (
                    'request' not in v
                    or (params.get('op') and v['request']['op'] != params['op'])
                    or v['request']['op'] in ('feedback.list', 'feedback.get')
                ):
                    continue
                full = _module('failure_contract').response(v['response'], v['request'])
                entries.append(
                    {
                        'request_id': key,
                        'op': v['request']['op'],
                        'method': v['request']
                        .get('params', {})
                        .get('action', {})
                        .get('method'),
                        'status': full['status'],
                        **{
                            name: copy.deepcopy(full[name])
                            for name in ('execution', 'infrastructure')
                            if name in full
                        },
                    }
                )
            offset = params.get('offset', 0)
            return {
                'items': entries[offset : offset + params.get('limit', 32)],
                'total': len(entries),
            }
        if op == 'task.progress':
            self._visible_task(params['task_id'])
            if self.task_stream:
                self.task_stream.require(params['task_id'], current=True)
            prior = self.task_progress.get(params['task_id'], {})
            if not self.task_stream and prior.get('state') in ('passed', 'abandoned'):
                raise ValueError(
                    'Recorded completion or abandonment cannot be overwritten outside a task stream'
                )
            activity = {**params, 'source': 'agent_choice'}
            progress = copy.deepcopy(self.task_progress)
            activities = copy.deepcopy(self.task_activity)
            activities[params['task_id']] = activity
            if prior.get('state') != 'passed':
                progress[params['task_id']] = activity
            self._persist_state(task_progress=progress, task_activity=activities)
            self.task_progress = progress
            self.task_activity = activities
            atomic_json(self.session.directory / 'task-progress.json', self.task_progress)
            return {**activity, 'completion_recorded': prior.get('state') == 'passed'}
        if op == 'server.recover':
            if self.evaluating:
                raise ValueError('Recovery cannot run inside an active acceptance window')
            if self.task_worlds is not None and params.get('reset_world', False):
                raise ValueError(
                    'A retained task workspace cannot be reset; ask the experiment host to reconcile its world receipt'
                )
            return _module('server_recovery').recover(self, **params)
        if op == 'environment.describe':
            description = {
                'protocol_version': VERSION,
                'actor': self.actor,
                'session_id': self.session.run_id,
                'hour_ticks': 72000,
                'interface_language': _contracts.INTERFACE_LANGUAGE,
                'server_bridge': self.session.server.url,
                'client_required': False,
                'client_interfaces_enabled': False,
                'real_player_join_mode': 'spectator',
                'fake_player_modes': 'unchanged; controlled by task evaluation',
                'shared_acceptance': {
                    'equipment_state': 'Equipment properties must be obtainable in survival; applies to every task',
                    'hard_expert_inputs': 'No preloaded TNT items, including nested inventories; placed TNT blocks are permitted',
                    'shulker_io': 'Tasks declaring shulker_io require automatic output packing and, when specified, automatic input unpacking. Count actual contents of full and partial boxes; empty shells earn no product credit.',
                    'spawners': 'Creating, supplying or cloning spawners is forbidden; naturally generated spawners may remain',
                    'spawner_exception': 'Explicit spawner conversion scenarios may receive one environment-provided spawner at world startup; agents cannot add more',
                    'equipment_telemetry_unavailable': 'inconclusive',
                },
                'evaluator_actor_requirements': {
                    'when': 'Task actor_policy requires a player',
                    'conditions': [
                        'alive',
                        'connected',
                        'non-spectator fake',
                        'declared position/dimension',
                        'unchanged native identity/death/connection counters',
                    ],
                    'site_check': 'Read-only collision/support/fluid check; no automatic protection or respawn',
                    'telemetry_unavailable': 'inconclusive',
                },
                'wiki_policy': 'information-only; tutorial namespaces and redirect aliases denied',
                'knowledge_access': {
                    'methods': sorted(KNOWLEDGE_METHODS),
                    'requires_game': False,
                    'requires_task': False,
                    'available_during_evaluation': True,
                    'execution_backend': 'direct',
                    'executor_preference': 'always_direct',
                    'query_logging': 'Request ledger and invocation trace; no game clock operations',
                },
                'schemas': SCHEMAS,
                'task_count': len(self._visible_tasks()),
                'task_selection': (
                    {'mode': 'task_stream', **self.task_stream.public_state()}
                    if self.task_stream
                    else {
                        **self.task_selection.metadata,
                        'configured_by': self.task_selection_source,
                    }
                ),
                'task_lists': [
                    {'difficulty': level, 'task_count': len(self._visible_tasks(level))}
                    for level in ('easy', 'medium', 'hard', 'expert')
                    if self._visible_tasks(level)
                ],
                'execution_backends': ['direct', 'cli', 'execution_agent'],
                'execution_agent_enabled': self.allow_execution_agent,
                'max_evaluation_volume': self.max_evaluation_volume,
                'max_evaluation_chunks': self.max_evaluation_chunks,
                'semantics': 'One explicit request produces one feedback. Only documented mechanical polling/leases/cleanup occur inside a primitive. No skill library or planning in environment.',
            }
            description['feedback_access'] = {
                'response': 'Optional detail=summary|full|reference and fields; full retained in ledger',
                'retrieve': 'feedback.get',
                'index': 'feedback.list',
                'schemas': 'schemas.get',
                'tools': 'actions.get',
            }
            description['evaluation_policy'] = {
                'default_mode': 'auto',
                'selection': 'Task-required structural review selects hybrid; otherwise deterministic',
                'explicit_modes': ['deterministic', 'hybrid'],
                'review_unavailable': 'inconclusive; required review is never skipped',
            }
            description['task_progress'] = copy.deepcopy(self.task_progress)
            description['agent_guidance'] = {
                'operating_rules': _module('guidance').AGENT_GUIDANCE,
                'knowledge_rules': _module('guidance').KNOWLEDGE_GUIDANCE,
                'public_procedures': _module('guidance').public_procedures(),
                'source': 'techmc_env',
                'version': 'environment-owned-v1',
            }
            description['task_completion_policy'] = {
                'guidance': TASK_COMPLETION_GUIDE,
                'completion_order': (
                    'host_delivered' if self.task_stream else 'learner_selected'
                ),
                'completion_evidence': 'valid independent evaluation pass',
                'round_definition': 'task_episode_v1',
                'experiment_ownership': 'The experiment runner controls the run and closes the shared session. Finishing one task attempt does not end the experiment.',
                'distinct_completion_key': 'task_id',
            }
            description['recovery_policy'] = {
                'automatic': 'One same-world attempt after a confirmed managed-server crash; uncertain actions are never replayed',
                'reset': 'Explicit server.recover reset_world=true; old worlds retained',
                'passed_tasks': 'Preserved independently of world changes',
            }
            resource_ref = getattr(self, '_resource_policy_ref', None)
            previous = self.ledger.get(resource_ref, {}).get('response', {})
            resource_recall = {
                'op': 'feedback.get',
                'params': {
                    'request_id': resource_ref or request_id,
                    'fields': ['result.resource_policy'],
                },
            }
            if previous.get('status') == 'completed' and previous.get('result', {}).get(
                'resource_policy', {}
            ).get('agent_instructions'):
                description['resource_policy'] = {
                    'view': 'reference',
                    'feedback_ref': resource_ref,
                    'recall': resource_recall,
                }
            else:
                resource_recall['params']['request_id'] = request_id
                description['resource_policy'] = {
                    **_module('resource_policy').describe(self),
                    'recall': resource_recall,
                }
                self._resource_policy_ref = request_id
            if params.get('detail') == 'manifest':
                return _module('discovery_views').manifest(self, description)
            if params.get('detail') == 'summary':
                description['schemas'] = {
                    'available': list(SCHEMAS),
                    'details_op': 'schemas.get',
                }
            return description
        if op == 'actions.list':
            tools = action_catalog()
            if params.get('category'):
                tools = [t for t in tools if t['category'] == params['category']]
            if params.get('prefix'):
                tools = [t for t in tools if t['method'].startswith(params['prefix'])]
            if params.get('detail') == 'index':
                tools = [
                    {
                        k: t[k]
                        for k in (
                            'method',
                            'description',
                            'category',
                            'requires_game',
                            'requires_task',
                            'effect',
                            'available_during_evaluation',
                            'execution_backends',
                        )
                    }
                    for t in tools
                ]
            return tools[
                params.get('offset', 0) : params.get('offset', 0) + params.get('limit', 256)
            ]
        if op == 'tasks.list':
            tasks = self._visible_tasks(params.get('difficulty'), params.get('world_type'))
            return (
                _module('discovery_views').task_index(tasks)
                if params.get('detail') == 'index'
                else tasks
            )
        if op == 'tasks.get':
            return self._validated(self._visible_task, params['task_id'])
        if op == 'task.start':
            if (
                not self.task_stream
                and self.task_progress.get(params['task_id'], {}).get('state') == 'abandoned'
            ):
                raise ValueError('Task was permanently abandoned by this experiment')
            if set(params) - {'task_id', 'profile_id'}:
                raise ValueError('Task goals/thresholds cannot be overridden in task.start')
            if self.task_stream:
                self.task_stream.require(params['task_id'], current=True)
            task = select_profile(
                self.task_selection.get(params['task_id']), params.get('profile_id')
            )
            if self.task_stream and self._active_task_instance is not None:
                active = self._instance(self._active_task_instance)
                if active['task']['id'] != task['id'] or active['task'].get(
                    'selected_profile'
                ) != task.get('selected_profile'):
                    raise ValueError(
                        'This visit already has a different task/profile binding; release it before another visit'
                    )
                projected = copy.deepcopy(active)
                self.task_stream.redact(projected['task'])
                projected.pop('task_selection_hash', None)
                return projected
            if self.evaluating:
                raise ValueError('Cannot start a task during an active acceptance window')
            workspace = (
                self.task_worlds.activate(task['id']) if self.task_worlds is not None else {}
            )
            instance = {
                'id': str(uuid.uuid4()),
                'task': task,
                'actor': self.actor,
                'server_bridge': self.session.server.url,
                'created_ns': time.time_ns(),
                'task_selection_hash': self.task_selection.selection_hash,
            }
            instance['server_instance_id'] = (
                _module('server_recovery').binding(self).get('instance_id')
            )
            if workspace:
                instance.update(
                    workspace_task_id=task['id'],
                    workspace_world_key=workspace['world_key'],
                    workspace_world=workspace['world'],
                )
                instance['server_instance_id'] = workspace['instance_id']
            if self.task_stream:
                instance['stream_position'] = self.task_stream.position
            atomic_json(ROOT / 'runs/task-instances' / f"{instance['id']}.json", instance)
            if self.task_stream:
                instances = self._stream_instances | {instance['id']}
                self._persist_state(
                    stream_instances=sorted(instances),
                    active_task_instance=instance['id'],
                    active_stream_position=self.task_stream.position,
                )
                self._stream_instances = instances
                self._active_task_instance = instance['id']
                self._active_stream_position = self.task_stream.position
                instance = copy.deepcopy(instance)
                self.task_stream.redact(instance['task'])
                instance.pop('task_selection_hash', None)
            return instance
        if op == 'action.execute':
            return self._primitive(params['action'], request_id)
        if op == 'submission.validate':
            submission = params['submission']
            instance = self._validated(self._instance, submission['task_instance_id'])
            manifest = self._validated(
                validate_submission,
                instance['task'],
                instance['id'],
                submission,
                self.max_evaluation_volume,
            )
            if params.get('preflight', False):
                if self.evaluating:
                    raise OperationNotStarted(
                        ValueError('Game preflight is unavailable during exclusive evaluation')
                    )
                return preflight_submission(
                    instance['task'],
                    instance['id'],
                    submission,
                    self.session.server,
                    self.max_evaluation_volume,
                )
            return {
                **manifest,
                'validation_scope': 'manifest_only',
                'world_preflight': {'status': 'not_requested'},
            }
        if op == 'evaluation.run':
            if self.evaluating:
                raise OperationNotStarted(
                    ValueError(
                        'Another acceptance window is active; wait for its report before starting a new evaluation'
                    )
                )
            submission = params['submission']
            instance = self._validated(self._instance, submission['task_instance_id'])
            self._validated(
                validate_submission,
                instance['task'],
                instance['id'],
                submission,
                self.max_evaluation_volume,
            )
            if set(params) - {'submission', 'mode', 'tick_mode'}:
                raise OperationNotStarted(
                    ValueError(
                        'Evaluation criteria and measurement windows cannot be overridden'
                    )
                )
            tick_mode = params.get('tick_mode', instance['task']['measurement']['mode'])
            if tick_mode not in instance['task']['measurement']['allowed_modes']:
                raise OperationNotStarted(ValueError('Tick mode is not permitted by this task'))
            policy = self._validated(
                _module('acceptance.modes').evaluation_mode,
                instance['task'],
                params.get('mode', 'auto'),
            )
            reviewer = None
            evaluation_id = 'eval-' + str(uuid.uuid4())
            child = None
            try:
                if policy['effective'] == 'hybrid':
                    try:
                        from runtime.models.transport import ensure_model_available

                        ensure_model_available(preflight=True)
                    except Exception as exc:
                        result = _module('failure_contract').interrupted_report(
                            instance,
                            submission,
                            evaluation_id,
                            ROOT / 'runs' / evaluation_id,
                            exc,
                        )
                        result['evaluation_mode'] = policy
                        result['metrics']['review_blocked'] = True
                        result['checks'][0].update(
                            id='review_unavailable',
                            message='LLM review is unavailable: '
                            + str(exc)
                            + '. Resolve credentials or host budget before a new evaluation.',
                        )
                        result['infrastructure'].update(
                            status='review_unavailable', stage='review'
                        )
                        return _module('failure_contract').report(result)
                    reviewer = _module('ai_review').AIReviewer()
                server = Bridge(
                    evaluation_id,
                    self.actor,
                    'server',
                    port=urlsplit(self.session.server.url).port,
                    timeout=15,
                    token_file=self.session.server.token_file,
                )
                client = Bridge(
                    evaluation_id,
                    self.actor,
                    'client',
                    port=urlsplit(self.session.client.url).port,
                    token_file=self.session.client.token_file,
                    timeout=15,
                )
                child = ActionSession(
                    self.actor,
                    evaluation_id,
                    server=server,
                    client=client,
                    client_required=False,
                )
                self._evaluation_children.append(child)
                self.used_game = True
                self.evaluating = True
                child.start_heartbeat()
                measured = Evaluator(
                    child,
                    self.max_evaluation_volume,
                    reviewer,
                    max_chunks=self.max_evaluation_chunks,
                ).run(instance, submission, tick_mode=tick_mode)
                measured['evaluation_mode'] = policy
                return _module('failure_contract').report(measured)
            except Exception as exc:
                result = _module('failure_contract').interrupted_report(
                    instance, submission, evaluation_id, ROOT / 'runs' / evaluation_id, exc
                )
                result['evaluation_mode'] = policy
                if child is not None:
                    try:
                        errors = child.cleanup()
                        if not isinstance(errors, list) or not all(
                            isinstance(error, str) for error in errors
                        ):
                            raise RuntimeError(
                                'Invalid cleanup feedback; expected a list of error strings'
                            )
                        result['cleanup_errors'] = errors
                    except Exception as cleanup:
                        result['cleanup_errors'] = [str(cleanup)]
                return result
            finally:
                self.evaluating = False
        if op == 'evaluation.get':
            evaluation_id = params['evaluation_id']
            if not isinstance(evaluation_id, str) or not all(
                c.isalnum() or c in '-_' for c in evaluation_id
            ):
                raise ValueError('Invalid evaluation ID')
            # A failed disk write must not make a live controller return an older
            # pre-recovery report. The ledger is authoritative for this attempt.
            report = next(
                (
                    copy.deepcopy(e['response']['result'])
                    for e in self.ledger.values()
                    if e.get('request', {}).get('op') == 'evaluation.run'
                    and isinstance(e.get('response', {}).get('result'), dict)
                    and e['response']['result'].get('evaluation_id') == evaluation_id
                ),
                None,
            )
            if report is None:
                report = json.loads(
                    (ROOT / 'runs' / evaluation_id / 'evaluation.json').read_text(
                        encoding='utf-8'
                    )
                )
            self.task_selection.require(report['task_id'])
            if self.task_stream:
                self.task_stream.require(report['task_id'])
            if (
                self.task_stream
                and report.get('task_instance_id') not in self._stream_instances
            ):
                retained = any(
                    m.get('evaluation_id') == evaluation_id
                    and m.get('task_instance_id') == report.get('task_instance_id')
                    for m in self.machines
                )
                retained = retained or any(
                    e.get('request', {}).get('op') == 'evaluation.run'
                    and isinstance(e.get('response', {}).get('result'), dict)
                    and e['response']['result'].get('evaluation_id') == evaluation_id
                    and e['response']['result'].get('task_instance_id')
                    == report.get('task_instance_id')
                    for e in self.ledger.values()
                )
                if not retained:
                    raise ValueError('Evaluation does not belong to this experiment')
            return _module('failure_contract').report(report)
        if op == 'session.close':
            if self.task_stream:
                raise ValueError(
                    'The experiment host owns session lifecycle during a task stream'
                )
            return self.close()
        raise ValueError('Unsupported operation')

    def handle(self, request):
        """接收一次公共请求，串联执行锁、超时记录与请求处理。

        请求内容、执行状态与回执进入会话账本，重复请求按已有记录处理。
        """
        with self._ledger_lock:
            self._inflight += 1
        try:
            if isinstance(request, dict) and self._mutation_request(request):
                with self._mutation_lock:
                    return self._handle(request)
            return self._handle(request)
        except Exception as exc:
            if isinstance(getattr(exc, 'execution_agent_failure', None), ExecutionAgentError):
                raise
            # Last boundary: reporting/recovery must not tear down a caller after
            # an operation may already have executed. Never redispatch here.
            contract = _module('failure_contract')
            rid = (
                request.get('request_id', 'invalid') if isinstance(request, dict) else 'invalid'
            )
            if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', rid):
                rid = 'invalid'
            result = {
                'protocol_version': VERSION,
                'request_id': rid,
                'session_id': self.session.run_id,
                'status': 'error',
                'result': None,
                'error': contract.fault(exc),
                'feedback_ref': rid,
                'infrastructure': {
                    'status': 'request_uncertain',
                    'stage': 'request',
                    'recovered': False,
                    'reset_available': False,
                    'fault': contract.fault(exc),
                },
                'execution': {
                    'state': 'unknown',
                    'retry': 'observe_before_new_request',
                    'next_steps': [
                        'Feedback finalization failed. Inspect retained feedback and world state before any mutation; no operation was replayed.'
                    ],
                },
            }
            prior = self.ledger.get(rid, {})
            original = prior.get('response', {}).get('error')
            if isinstance(original, dict):
                result['infrastructure']['fault'] = {
                    'type': str(original.get('type', 'UnknownError')),
                    'message': str(original.get('message', ''))[:4000],
                }
                result['infrastructure']['state_error'] = contract.fault(exc)
            validate('response', result)
            # Retain an existing canonical record, rather than replacing evidence
            # with this boundary error. A fresh request can query feedback.get.
            return result
        finally:
            with self._ledger_lock:
                self._inflight -= 1

    @staticmethod
    def _mutation_request(request):
        return _action_catalog.request_effect(request) != 'read_only'

    def _handle(self, request):
        contract = _module('failure_contract')
        request_id = (
            request.get('request_id', 'invalid') if isinstance(request, dict) else 'invalid'
        )
        if not isinstance(request_id, str) or not re.fullmatch(
            r'[A-Za-z0-9_.:-]{1,128}', request_id
        ):
            request_id = 'invalid'
        response = {
            'protocol_version': VERSION,
            'request_id': request_id,
            'session_id': self.session.run_id,
            'status': 'error',
            'result': None,
            'error': None,
        }
        fingerprint = None
        dispatched = False
        local_diagnostic = False
        owned_entry = False
        known_state = None
        executor_interruption = None
        canonical_persisted = False
        progress_before = copy.deepcopy(self.task_progress)
        try:
            json.dumps(request, allow_nan=False)
            validate('request', request)
            local_diagnostic = (
                request['op'] == 'action.execute'
                and request['params'].get('action', {}).get('method') in LOCAL_DIAGNOSTICS
            )
            if self.closed:
                raise ValueError('Environment session is closed')
            fingerprint = contract.request_hash(request)
            with self._ledger_lock:
                # 请求身份、内容指纹和已有回执在账本中一起读取，重复请求沿已有状态继续。
                previous = self.ledger.get(request_id)
                if previous:
                    if (
                        previous['fingerprint'] not in (fingerprint, stable_hash(request))
                        and contract.request_hash(previous.get('request', {})) != fingerprint
                    ):
                        raise ValueError('Request ID reused with another payload')
                    if self._mutation_request(request) and (
                        self.task_worlds is not None or previous.get('workspace_world_key')
                    ):
                        active = self.task_workspace_status().get('active') or {}
                        if previous.get('workspace_world_key') != active.get('world_key') or (
                            self.task_stream
                            and previous.get('stream_position') != self.task_stream.position
                        ):
                            raise ValueError(
                                'Mutation request belongs to another task workspace or visit; use feedback.get to inspect its historical result'
                            )
                    prior = contract.response(previous['response'], request)
                    # Only a native, same-generation journal lookup may redispatch.
                    lookup = prior.get('execution', {}).get('retry') == 'same_request_only'
                    if prior['status'] == 'pending_or_unknown' and lookup:
                        try:
                            current = (
                                _module('server_recovery').binding(self).get('instance_id')
                            )
                        except Exception as exc:
                            info = prior.setdefault(
                                'infrastructure',
                                {'status': 'request_uncertain', 'recovered': False},
                            )
                            info['state_error'] = contract.fault(exc)
                            current = None
                        if not current or current != previous.get('server_instance_id'):
                            prior['execution']['retry'] = 'observe_before_new_request'
                            prior['execution']['next_steps'] = [
                                'World identity is changed or unverified. Inspect state; the uncertain operation was not replayed.'
                            ]
                            self.ledger[request_id]['response'] = prior
                            self._save_ledger()
                            return contract.view(prior, request.get('response'))
                    else:
                        return contract.view(prior, request.get('response'))
                response.update(
                    status='pending_or_unknown',
                    execution=contract.execution(request, {'status': 'pending_or_unknown'}),
                )
                if not previous:
                    self.ledger[request_id] = {
                        'fingerprint': fingerprint,
                        'request': copy.deepcopy(request),
                        'server_instance_id': None,
                        'response': copy.deepcopy(response),
                    }
                owned_entry = True
                self._save_ledger()
            needs_binding = request['op'] in ('evaluation.run', 'task.start') or (
                request['op'] == 'action.execute'
                and request['params']['action'].get('side') != 'local'
            )
            origin = _module('server_recovery').binding(self) if needs_binding else {}
            workspace = self.task_workspace_status().get('active') or {}
            self.ledger[request_id].update(
                server_instance_id=origin.get('instance_id'),
                server_world=origin.get('world'),
                workspace_world_key=workspace.get('world_key'),
                stream_position=self.task_stream.position if self.task_stream else None,
            )
            if request['op'] == 'action.execute':
                action = request['params']['action']
                if (
                    self.evaluating
                    and action['method'] not in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS
                ):
                    raise ValueError(
                        'Acceptance window is exclusive; only knowledge queries are available'
                    )
                check_action(action)
            dispatched = True
            result = public_result(self._dispatch(request['op'], request['params'], request_id))
            if request['op'] == 'task.start' and isinstance(result, dict):
                self.ledger[request_id].update(
                    workspace_world_key=result.get('workspace_world_key'),
                    server_instance_id=result.get('server_instance_id'),
                )
                if result.get('workspace_world') is not None:
                    self.ledger[request_id]['server_world'] = result['workspace_world']
            response.update(status='completed', result=result, error=None)
            if request['op'] == 'action.execute' and isinstance(result, dict):
                payload = result.get('result', {})
                if (
                    isinstance(payload, dict)
                    and payload.get('status') == 'running'
                    and not request['params']['action'].get('wait', True)
                ):
                    response['status'] = 'running'
        except BaseException as exc:
            executor_failure = getattr(exc, 'execution_agent_failure', None)
            if isinstance(executor_failure, ExecutionAgentError):
                executor_interruption = exc
                exc = executor_failure
            elif not isinstance(exc, Exception):
                raise
            transport = (
                isinstance(exc, (OSError, TimeoutError, ConnectionError))
                or type(exc).__name__ == 'TimeoutExpired'
            )
            if isinstance(exc, (BridgeError, OperationNotStarted)):
                known_state = exc.execution_state
            if isinstance(exc, LocalExecutionError):
                action = request.get('params', {}).get('action', {})
                known_state = exc.state_for(
                    {
                        'method': action.get('method'),
                        'params': action.get('params'),
                        'run_id': self.session.run_id,
                        'actor_id': self.actor,
                        'request_id': request_id,
                    }
                )
            uncertain = (
                dispatched
                and isinstance(request, dict)
                and request.get('op') == 'action.execute'
                and (transport or isinstance(exc, BridgeError))
                and known_state not in ('not_started', 'completed')
            )
            response.update(
                status='pending_or_unknown' if uncertain else 'error', error=contract.fault(exc)
            )
            if isinstance(exc, BridgeError) and exc.native_feedback is not None:
                response['result'] = copy.deepcopy(exc.native_feedback)
                if owned_entry:
                    self.ledger[request_id]['native_feedback'] = copy.deepcopy(
                        exc.native_feedback
                    )
                    self.ledger[request_id]['native_request'] = copy.deepcopy(
                        exc.native_request
                    )
            if isinstance(exc, LocalExecutionError):
                response['result'] = copy.deepcopy(exc.local_feedback)
                if owned_entry:
                    self.ledger[request_id]['local_feedback'] = copy.deepcopy(
                        exc.local_feedback
                    )
                    self.ledger[request_id]['local_request'] = copy.deepcopy(exc.local_request)
            if isinstance(exc, ExecutionAgentError):
                response['result'] = copy.deepcopy(exc.executor_evidence)
            if type(exc).__name__ == 'TaskWorldError':
                response['infrastructure'] = {
                    'status': 'request_uncertain',
                    'stage': 'request',
                    'recovered': False,
                    'reset_available': False,
                    'fault': contract.fault(exc),
                    'note': 'Task-world transition is blocked or uncertain. The host must inspect its original lifecycle receipt; no new world switch was retried.',
                }
            # A runtime ValueError can follow side effects. Only pre-dispatch
            # validation establishes that the operation never started.
        req = request if isinstance(request, dict) else {}
        response['execution'] = contract.execution(req, response, dispatched, known_state)
        if response['status'] == 'pending_or_unknown' and owned_entry:
            action = req.get('params', {}).get('action', {})
            if (
                action.get('side') == 'server'
                and action.get('executor', 'direct') != 'execution_agent'
                and self.ledger[request_id].get('server_instance_id')
            ):
                response['execution']['retry'] = 'same_request_only'
                response['execution']['next_steps'] = [
                    'Retry only this exact request ID and payload to consult native deduplication, provided the world identity is unchanged.'
                ]
        game_request = req.get('op') == 'evaluation.run' or (
            req.get('op') == 'action.execute'
            and req.get('params', {}).get('action', {}).get('side') != 'local'
        )
        review_blocked = contract.is_report(response.get('result')) and response['result'][
            'metrics'
        ].get('review_blocked', False)
        if (
            not local_diagnostic
            and dispatched
            and self.used_game
            and game_request
            and not review_blocked
            and known_state not in ('not_started', 'completed')
        ):
            try:
                incident = _module('server_recovery').after_request(
                    self, req, copy.deepcopy(response)
                )
                if incident:
                    validate('infrastructure', incident)
                    response['infrastructure'] = incident
            except Exception as exc:
                response['infrastructure'] = {
                    'status': 'recovery_error',
                    'recovered': False,
                    'reset_available': False,
                    'stage': 'recovery',
                    'recovery_error': contract.fault(exc),
                }
        if req.get('op') == 'server.recover' and response['status'] == 'completed':
            response['infrastructure'] = copy.deepcopy(response['result'])
        if (
            req.get('op') == 'feedback.get'
            and req.get('params', {}).get('request_id') in self.ledger
        ):
            source_id = req['params']['request_id']
            source = contract.response(
                self.ledger[source_id]['response'], self.ledger[source_id].get('request')
            )
            response['source_request_id'] = source_id
            if source.get('execution'):
                response['source_execution'] = source['execution']
            if source.get('infrastructure'):
                response['infrastructure'] = copy.deepcopy(source['infrastructure'])
        if contract.is_report(response.get('result')):
            measured = contract.report(response['result'])
            if response.get('infrastructure'):
                measured['infrastructure'] = {
                    **measured.get('infrastructure', {}),
                    **response['infrastructure'],
                }
            measured = contract.report(measured)
            response['result'] = measured
            if measured.get('infrastructure'):
                response['infrastructure'] = copy.deepcopy(measured['infrastructure'])
        if response.get('error') and response.get('infrastructure'):
            response['infrastructure'].setdefault('fault', copy.deepcopy(response['error']))
        if (
            response['status'] == 'pending_or_unknown'
            or (
                game_request
                and response.get('error')
                and response['execution']['state'] == 'unknown'
            )
        ) and 'infrastructure' not in response:
            response['infrastructure'] = {
                'status': 'request_uncertain',
                'recovered': False,
                'reset_available': False,
                'stage': 'request',
                'fault': copy.deepcopy(response['error']),
            }
        # Recovery exceptions cannot erase an already produced independent report.
        if (
            req.get('op') == 'evaluation.run'
            and response['status'] == 'completed'
            and contract.is_report(response.get('result'))
        ):
            try:
                contract.persist_report(ROOT, response['result'])
                self._record_machine(req, response['result'])
            except Exception as exc:
                measured = response['result']
                info = measured.setdefault(
                    'infrastructure', {'status': 'persistence_failed', 'recovered': False}
                )
                info['persistence_error'] = contract.fault(exc)
                info['measurement_valid'] = False
                measured = contract.report(measured)
                response['result'] = measured
                response['infrastructure'] = copy.deepcopy(measured['infrastructure'])
                try:
                    contract.persist_report(ROOT, measured)
                except Exception:
                    pass  # The returned persistence error makes no durability claim.
                try:
                    self._record_machine(req, measured)
                except Exception:
                    pass
        response['execution'] = (
            contract.execution(req, response, dispatched, known_state)
            if response['status'] != 'pending_or_unknown'
            else response['execution']
        )
        response = contract.response(response, req)
        if owned_entry:
            self.ledger[request_id]['response'] = copy.deepcopy(response)
            try:
                self._save_ledger()
                canonical_persisted = True
            except Exception as exc:
                info = response.setdefault(
                    'infrastructure', {'status': 'persistence_failed', 'recovered': False}
                )
                info['persistence_error'] = contract.fault(exc)
                if req.get('op') == 'evaluation.run' and contract.is_report(
                    response.get('result')
                ):
                    info['measurement_valid'] = False
                    response['result']['infrastructure'] = copy.deepcopy(info)
                    response['result'] = contract.report(response['result'])
                    self.task_progress = progress_before
                    try:
                        atomic_json(
                            self.session.directory / 'task-progress.json', self.task_progress
                        )
                        contract.persist_report(ROOT, response['result'])
                        self._record_machine(req, response['result'])
                    except Exception:
                        pass
                response['execution']['retry'] = 'none'
                response['execution']['next_steps'] = [
                    'Feedback persistence failed. Retain this response and report to the host; do not repeat the operation.'
                ]
                self.ledger[request_id]['response'] = copy.deepcopy(response)
        if executor_interruption is not None:
            if canonical_persisted:
                from audit.environment_evidence import attach_environment_feedback

                attach_environment_feedback(executor_interruption, req, response)
            raise executor_interruption
        return contract.view(response, req.get('response') if fingerprint is not None else None)

    def raw_feedback(self, request_id, request=None):
        if not isinstance(request_id, str):
            return None
        entry = self.ledger.get(request_id, {})
        if request is not None:
            try:
                contract = _module('failure_contract')
                if entry.get('fingerprint') not in (
                    stable_hash(request),
                    contract.request_hash(request),
                ) and contract.request_hash(entry.get('request', {})) != contract.request_hash(
                    request
                ):
                    return None
            except (ValueError, TypeError):
                return None
        return (
            _module('failure_contract').response(entry['response'], entry.get('request'))
            if entry.get('response')
            else None
        )

    def _record_machine(self, request, report):
        submission = request['params']['submission']
        entry = self.ledger.get(request.get('request_id'), {})
        binding = {
            'world': entry.get('server_world'),
            'instance_id': entry.get('server_instance_id'),
        }
        old = next(
            (m for m in self.machines if m['evaluation_id'] == report['evaluation_id']), {}
        )
        record = {
            'task_id': report['task_id'],
            'task_instance_id': report['task_instance_id'],
            'evaluation_id': report['evaluation_id'],
            'verdict': report['verdict'],
            'submission': copy.deepcopy(submission),
            'world': binding.get('world'),
            'server_instance_id': binding.get('instance_id'),
            'evidence_path': report['evidence_path'],
        }
        record['report'] = copy.deepcopy(report)
        record['workspace_world_key'] = entry.get('workspace_world_key')
        record['stream_position'] = entry.get('stream_position')
        if report.get('infrastructure'):
            record['infrastructure'] = copy.deepcopy(report['infrastructure'])
        progress = copy.deepcopy(self.task_progress)
        if report['verdict'] == 'pass':
            progress[report['task_id']] = {
                'state': 'passed',
                'evaluation_id': report['evaluation_id'],
                'source': 'independent_evaluation',
            }
            if old.get('world_checkpoint'):
                record['world_checkpoint'] = copy.deepcopy(old['world_checkpoint'])
            else:
                try:
                    record['world_checkpoint'] = _module('server_recovery').checkpoint(
                        self, report['evaluation_id']
                    )
                except Exception as exc:
                    record['world_checkpoint'] = {'status': 'archive_error', 'error': str(exc)}
        machines = [
            m for m in self.machines if m['evaluation_id'] != report['evaluation_id']
        ] + [record]
        atomic_json(self.session.directory / 'machines.json', machines)
        if progress != self.task_progress:
            atomic_json(self.session.directory / 'task-progress.json', progress)
        self._persist_state(machines=machines, task_progress=progress)
        self.machines = machines
        self.task_progress = progress

    def close(self):
        if self.closed:
            return {'closed': True, 'cleanup_errors': []}
        self.closed = True
        return {
            'closed': True,
            'cleanup_errors': self.session.cleanup() if self.used_game else [],
        }
