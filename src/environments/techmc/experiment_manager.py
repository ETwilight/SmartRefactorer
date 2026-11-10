"""实验资源生命周期：准备实例、启动、维护心跳、切换任务世界、保存和收尾。

Experiment 保存宿主记录与服务器状态；任务访问使用 HostTaskWorlds 记录世界身份，
本地进程和远程命令分别由对应的执行分支处理。
"""

from .paths import SOURCE_ROOT
import json
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

if __package__:
    from .remote_host import for_port, RemotePrelaunchRejected
    from .experiment_registry import (
        credentials,
        directory,
        write,
        authenticate,
        heartbeat,
        HostLease,
        task_world_lease,
        _read_json_retry,
        retained_instance,
        retained_resume_target,
        process_alive as _process_alive,
    )
    from .task_worlds import HostTaskWorlds
    from .host_resources import admission, memory, process_memory, CapacityUnavailable
    from .task_selection import TaskSelection
else:
    from remote_host import for_port, RemotePrelaunchRejected
    from experiment_registry import (
        credentials,
        directory,
        write,
        authenticate,
        heartbeat,
        HostLease,
        task_world_lease,
        _read_json_retry,
        retained_instance,
        retained_resume_target,
        process_alive as _process_alive,
    )
    from task_worlds import HostTaskWorlds
    from host_resources import admission, memory, process_memory, CapacityUnavailable
    from task_selection import TaskSelection

ROOT = Path(__file__).resolve().parents[2] / "assets/techmc"


def _unlinked(path):
    if path.is_symlink() or (
        path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400
    ):
        raise ValueError('Linked instance storage cannot prove an uninitialized world')


def uninitialized_world_proof(
    root, port, experiment_id, token, state, *, task_world_state=None
):
    """Local-only proof for a stopped fresh allocation with nothing to save.

    This never deletes data or marks a world saved. Callers must not translate
    remote host paths into local paths. A historical reconciliation can persist
    this proof in a NEW receipt and use the existing authenticated lease API.
    """
    root = Path(root).resolve()
    port = int(port)
    expected_owner = hashlib.sha256(token.strip().encode()).hexdigest()
    if not isinstance(state, dict) or state.get('experiment_id') != experiment_id:
        raise PermissionError('Foreign allocation binding')
    if state.get('owner_hash') != expected_owner:
        raise PermissionError('Missing or foreign allocation owner')
    authenticate(state, token)
    if (
        state.get('status') != 'stopped'
        or state.get('fresh_world') is not True
        or state.get('resumed_existing_world')
    ):
        raise ValueError('Allocation is not a stopped first fresh world')
    if (
        state.get('process_alive')
        or state.get('supervisor_lock_exists')
        or state.get('platform')
    ):
        raise ValueError('Allocation process or initialized platform remains')
    instance_id = state.get('instance_id')
    if not isinstance(instance_id, str) or str(uuid.UUID(instance_id)) != instance_id:
        raise ValueError('Invalid owned instance identity')
    base = root / 'runtime/servers' / str(port) / 'instances'
    target = base / instance_id
    for path in (base.parent.parent, base.parent, base, target):
        _unlinked(path)
    target = target.resolve()
    if target.parent != base.resolve() or Path(state.get('directory', '')).resolve() != target:
        raise ValueError('Instance directory does not match the owned allocation')
    world = Path(state.get('world', ''))
    _unlinked(world)
    if world.resolve() != target / 'world':
        raise ValueError('World directory escapes the owned instance')
    canonical = _read_json_retry(target / 'instance.json')
    current = _read_json_retry(base.parent / 'current.json')
    for receipt in (canonical, current):
        if receipt.get('owner_hash') != expected_owner:
            raise PermissionError('Missing or foreign instance owner')
        authenticate(receipt, token)
        for key in (
            'experiment_id',
            'instance_id',
            'directory',
            'world',
            'pid',
            'supervisor_pid',
            'fresh_world',
            'status',
        ):
            if receipt.get(key) != state.get(key):
                raise ValueError('Current and instance identity evidence disagree')
    experiment_dir = directory(root, experiment_id)
    native = _read_json_retry(experiment_dir / 'experiment.json')
    if native.get('owner_hash') != expected_owner:
        raise PermissionError('Missing or foreign experiment owner')
    authenticate(native, token)
    if native.get('experiment_id') != experiment_id or native.get('instance_id') != instance_id:
        raise PermissionError('Experiment receipt belongs to another allocation')
    manager = _read_json_retry(experiment_dir / 'manager.json', missing_ok=True)
    if manager and manager.get('experiment_id') != experiment_id:
        raise PermissionError('Foreign manager receipt')
    if manager.get('started_ns'):
        raise ValueError('Experiment already reached running state')
    controls = experiment_dir / 'task-world-control'
    for document in (
        task_world_state,
        manager.get('task_worlds'),
        _read_json_retry(controls / 'worlds.json', missing_ok=True),
    ):
        if document and any(document.get(key) for key in ('worlds', 'active', 'pending')):
            raise ValueError('Retained or pending task worlds require preservation')
    if list((controls / 'requests').glob('*.json')) or list(
        (controls / 'receipts').glob('*.json')
    ):
        raise ValueError('Task-world transition history requires explicit reconciliation')
    locks = [
        base.parent / 'supervisor.lock',
        root / 'runtime/reservations' / (str(port) + '.json'),
    ]
    if any(path.exists() for path in locks):
        raise ValueError('Allocation process locks or reservation remain')
    processes = {key: state.get(key) for key in ('pid', 'supervisor_pid')}
    if any(_process_alive(pid) for pid in processes.values()):
        raise ValueError('Recorded allocation process is still alive')
    properties = {}
    for line in (target / 'server.properties').read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            properties[key.strip()] = value.strip()
    if properties.get('level-name') != 'world':
        raise ValueError('Configured game world differs from owned world')
    entries = []
    if world.exists():
        if not world.is_dir():
            raise ValueError('World path is not a directory')
        for path in world.iterdir():
            _unlinked(path)
            entries.append(path.name)
            if path.name not in ('carpet.conf', 'datapacks'):
                raise ValueError('World already contains game data: ' + path.name)
            if path.name == 'carpet.conf' and not path.is_file():
                raise ValueError('Invalid prepared configuration')
            if path.name == 'datapacks':
                if not path.is_dir():
                    raise ValueError('Invalid prepared datapack directory')
                for nested in path.rglob('*'):
                    _unlinked(nested)
    return {
        'reason': 'fresh_world_never_initialized',
        'save_required': False,
        'save_confirmed': False,
        'verified_ns': time.time_ns(),
        'experiment_id': experiment_id,
        'instance_id': instance_id,
        'directory': str(target),
        'world': str(world.resolve()),
        'world_entries': sorted(entries),
        'terminated_processes': processes,
        'task_worlds_empty': True,
        'allocation_locks_absent': True,
    }


class Experiment:
    def __init__(
        self,
        port=18776,
        heap_mb=2048,
        processors=1,
        root=ROOT,
        experiment_id=None,
        world_type='void',
        task_selection=None,
        task_worlds=False,
        resume_saved=None,
    ):
        self.root = Path(root)
        self.port = port
        self.heap_mb = heap_mb
        self.processors = processors
        self.world_type = world_type
        self.task_selection = TaskSelection(task_selection, world_type=world_type)
        self.id = experiment_id or str(uuid.uuid4())
        self.folder = directory(self.root, self.id)
        previous = None
        if resume_saved is not None and not task_worlds:
            raise ValueError(
                'Explicit checkpoint resume requires retained task-world lifecycle'
            )
        recovery = None
        if resume_saved is not None and resume_saved.get('recovery_receipt'):
            from .explicit_resume import verify_unstarted_recovery

            recovery = verify_unstarted_recovery(
                resume_saved['recovery_receipt'], resume_saved, root=self.root
            )
        if (self.folder / 'manager.json').exists():
            if resume_saved is None:
                raise FileExistsError(
                    'Experiment ID already has a record; start a new batch with a new ID'
                )
            original = (self.folder / 'manager.json').read_bytes()
            expected_sha = (
                recovery['live_manager_sha256'] if recovery else resume_saved['manager_sha256']
            )
            if hashlib.sha256(original).hexdigest() != expected_sha:
                raise ValueError('Saved host manager changed after checkpoint')
            for attempt in (self.folder / 'resume-attempts').glob('*/manager.json'):
                value = _read_json_retry(attempt)
                if (
                    value.get('status') not in ('completed', 'failed', 'cancelled')
                    or value.get('resource_released') is not True
                    or value.get('launch_state') == 'unknown'
                ):
                    raise RuntimeError(
                        'Unresolved prior resume attempt requires explicit reconciliation: '
                        + str(attempt)
                    )
            previous = json.loads(original)
            if (
                previous.get('experiment_id'),
                previous.get('port'),
                previous.get('task_selection'),
            ) != (self.id, port, self.task_selection.spec):
                raise ValueError('Explicit resume host identity/selection mismatch')
            if not recovery and (
                previous.get('status') not in ('completed', 'failed', 'cancelled')
                or not previous.get('save_confirmed')
                or not previous.get('resource_released')
                or previous.get('cleanup_errors')
            ):
                raise ValueError('Explicit host resume requires a clean saved release')
            if recovery and previous.get('status') != 'cleanup_failed':
                raise ValueError('Recovery receipt does not match a failed unstarted manager')
        elif resume_saved is not None:
            raise ValueError('Explicit resume host record is missing')
        self.token_file, self.owner_hash = credentials(self.root, self.id)
        self.token = self.token_file.read_text().strip()
        self.remote = for_port(port, self.root)
        if self.remote:
            self.remote.owner_token_file = self.token_file
            self.remote.experiment_id = self.id
        self._resume_instance = None
        self._launch_id = str(uuid.uuid4()) if previous is not None else None
        if previous is not None:
            retained_id = (
                resume_saved['binding']['server_instance_id']
                if recovery
                else previous['instance_id']
            )
            retained_world = resume_saved['manager']['world'] if recovery else previous['world']
            if previous.get('instance_id', retained_id) != retained_id:
                raise ValueError('Failed manager changed the retained world')
            if self.remote:
                if recovery:
                    raise ValueError(
                        'Remote zero-start recovery requires an explicit remote proof receipt'
                    )
                proof = self.remote.retained_resume_proof(retained_id, self.id, self.token)
                if proof.get('verified') is not True:
                    raise ValueError('Remote retained world proof is not verified')
                state = proof['instance']
            else:
                state = retained_resume_target(
                    self.root, port, retained_id, self.id, self.token
                )
                if recovery:
                    current = _read_json_retry(
                        self.root / 'runtime/servers' / str(port) / 'current.json'
                    )
                    authenticate(current, self.token)
            if (
                state.get('owner_hash') != self.owner_hash
                or state.get('experiment_id') != self.id
            ):
                raise PermissionError(
                    'Explicit resume requires exact retained target owner identity'
                )
            if (
                state.get('instance_id') != retained_id
                or state.get('world') != retained_world
                or state.get('status') != 'stopped'
                or state.get('exit_code') != 0
                or state.get('shutdown_save_confirmed') is not True
            ):
                raise ValueError('Original saved target world differs from checkpoint')
            if recovery:
                if (
                    current.get('owner_hash') != self.owner_hash
                    or current.get('experiment_id') != self.id
                    or current.get('instance_id') != retained_id
                    or current.get('world') != retained_world
                    or current.get('status') != 'stopped'
                    or current.get('process_alive')
                    or (self.root / 'runtime/servers' / str(port) / 'supervisor.lock').exists()
                ):
                    raise ValueError('Signed zero-start recovery current allocation changed')
            if recovery:
                if (
                    hashlib.sha256(
                        (
                            self.root / 'runtime/servers' / str(port) / 'current.json'
                        ).read_bytes()
                    ).hexdigest()
                    != recovery['current_sha256']
                    or hashlib.sha256(
                        (Path(state['directory']) / 'instance.json').read_bytes()
                    ).hexdigest()
                    != recovery['instance_sha256']
                ):
                    raise ValueError('Recovered shutdown evidence changed after signed receipt')
                if any(
                    (self.root / 'runtime' / name / (str(port) + '.json')).exists()
                    for name in ('reservations', 'task-world-owners')
                ):
                    raise ValueError('Recovered allocation reservation has not been released')
            controls = _read_json_retry(self.folder / 'task-world-control/worlds.json')
            if controls.get('pending'):
                raise ValueError('Original host retains a pending task transition')
            active = controls.get('active')
            if active and (
                active.get('instance_id'),
                active.get('world'),
                active.get('world_key'),
            ) != (retained_id, state['world'], resume_saved['binding']['workspace_world_key']):
                raise ValueError(
                    'Saved active logical task world differs from explicit checkpoint'
                )
            if controls != previous.get('task_worlds'):
                raise ValueError('Saved task-world mapping changed')
            snapshot = self.folder / 'resume-history' / ('manager-' + expected_sha + '.json')
            snapshot.parent.mkdir(exist_ok=True)
            if snapshot.exists():
                if snapshot.read_bytes() != original:
                    raise ValueError('Resume history snapshot differs')
            else:
                with snapshot.open('xb') as stream:
                    stream.write(original)
            self._resume_instance = state['instance_id']
        if self.remote:
            self.remote.owner_token_file = self.token_file
            self.remote.experiment_id = self.id
        self.env = {
            **os.environ,
            'MC_EXPERIMENT_ID': self.id,
            'MC_EXPERIMENT_TOKEN_FILE': str(self.token_file),
            'MC_AGENT_SERVER_PORT': str(port),
            'TECHMC_REMOTE_CONFIG': str(
                Path(
                    os.environ.get(
                        'TECHMC_REMOTE_CONFIG', self.root / 'runtime/remote-servers.json'
                    )
                ).resolve()
            ),
        }
        if self._launch_id:
            self.env['MC_EXPERIMENT_LAUNCH_ID'] = self._launch_id
        self._resume_staged = previous is not None
        self._original_manager_sha = expected_sha if previous is not None else None
        self._resume_attempt_path = (
            (self.folder / 'resume-attempts' / self._launch_id / 'manager.json')
            if previous is not None
            else None
        )
        self._startup_epoch_confirmed = False
        self._launch_dispatch_attempted = False
        self._lease_acquisition_unknown = False
        self._resume_world = state['world'] if previous is not None else None
        self.record = {
            'experiment_id': self.id,
            'status': 'requested',
            'port': port,
            'heap_mb': heap_mb,
            'world_type': world_type,
            'task_selection': self.task_selection.spec,
            'task_selection_hash': self.task_selection.selection_hash,
            'created_ns': time.time_ns(),
            'retention': 'keep_until_explicit_prune',
        }
        if previous is not None:
            self.record.update(
                resume_from={
                    'manager_sha256': resume_saved['manager_sha256'],
                    'source_run': resume_saved['source_run'],
                    'instance_id': self._resume_instance,
                    'history_snapshot': str(snapshot),
                    'live_manager_sha256': expected_sha,
                },
                task_selection=previous['task_selection'],
            )
            self.record['launch_id'] = self._launch_id
            if recovery:
                self.record.update(
                    resume_recovery={
                        'reason': 'failed_unstarted_reconciled',
                        'receipt': resume_saved['recovery_receipt'],
                        'owner_hmac_sha256': recovery['owner_hmac_sha256'],
                    }
                )
        self.stopped = threading.Event()
        self.tunnel = None
        self.server = None
        self.allocated = False
        self.child = None
        self.task_worlds_enabled = bool(task_worlds)
        self._task_world_lease = False
        self._switch_lock = threading.RLock()
        self._switch_thread = None
        self._finishing = False
        self.task_worlds = None
        if self.task_worlds_enabled:
            self.task_worlds = HostTaskWorlds(
                self.folder / 'task-world-control', self._switch_task_world
            )
            self.env['MC_TASK_WORLD_CONTROL_DIR'] = str(self.task_worlds.directory.resolve())
        self.save_record()

    @property
    def record_path(self):
        return self._resume_attempt_path or self.folder / 'manager.json'

    def save_record(self):
        attempt = getattr(self, '_resume_attempt_path', None)
        if attempt:
            write(attempt, self.record)
        if not getattr(self, '_resume_staged', False):
            write(self.folder / 'manager.json', self.record)

    def _confirm_startup_epoch(self, state):
        if state.get('experiment_id') != self.id or state.get('launch_id') != self._launch_id:
            return False
        native = (
            self.remote.experiment_state()
            if self.remote
            else _read_json_retry(self.folder / 'experiment.json')
        )
        if native.get('launch_id') != self._launch_id:
            return False
        for document in (state, native):
            if (
                document.get('experiment_id') != self.id
                or document.get('owner_hash') != self.owner_hash
                or document.get('instance_id') != self._resume_instance
                or document.get('world') != self._resume_world
            ):
                raise PermissionError('Resume startup epoch has a different owner/world')
        self._startup_epoch_confirmed = True
        self.record['startup_epoch_confirmed'] = True
        self.record['server_started'] = True
        self.record['launch_state'] = 'acknowledged'
        return True

    def _publish_ready_record(self):
        if getattr(self, '_resume_staged', False):
            if not self._startup_epoch_confirmed:
                raise RuntimeError('Resume READY has no verified startup epoch')
            with HostLease(self.root, timeout=5):
                current = (self.folder / 'manager.json').read_bytes()
                if hashlib.sha256(current).hexdigest() != self._original_manager_sha:
                    raise RuntimeError(
                        'Original canonical manager changed before READY publication'
                    )
                self._resume_staged = False
                try:
                    self.save_record()
                except BaseException:
                    self._resume_staged = True
                    raise
        else:
            self.save_record()

    def _finish_unpublished_resume(self, outcome):
        if not self._resume_staged:
            return None
        errors = []
        if self._launch_dispatch_attempted:
            try:
                if self._confirm_startup_epoch(self.state()):
                    return None
            except Exception as exc:
                errors.append('startup evidence: ' + str(exc))
        if self._launch_dispatch_attempted or self._lease_acquisition_unknown:
            errors.append(
                'Unconfirmed startup or lease; retain attempt evidence and ownership, do not assume zero start'
            )
            self.record.update(
                status='cleanup_failed',
                finished_ns=time.time_ns(),
                requested_outcome=outcome,
                launch_state='unknown' if self._launch_dispatch_attempted else 'not_started',
                server_started=None if self._launch_dispatch_attempted else False,
                resource_released=False,
                save_confirmed=False,
                cleanup_errors=errors,
                capacity_declined=False,
            )
        else:
            if self._task_world_lease:
                try:
                    if self.remote:
                        self.remote.task_world_lease(release=True)
                    else:
                        with HostLease(self.root, timeout=5):
                            task_world_lease(
                                self.root, self.port, self.id, self.token, release=True
                            )
                    self._task_world_lease = False
                except Exception as exc:
                    errors.append('task-world lease: ' + str(exc))
            self.record.update(
                status=outcome if not errors else 'cleanup_failed',
                finished_ns=time.time_ns(),
                requested_outcome=outcome,
                launch_state='not_started',
                server_started=False,
                resource_released=not self._task_world_lease,
                save_confirmed=False,
                cleanup_errors=errors,
            )
        if self.task_worlds:
            self.record['task_worlds'] = self.task_worlds.state
        self.stopped.set()
        self._finished = True
        self.save_record()
        return self.record

    def state(self):
        if self.remote:
            return self.remote.state(fresh=True)
        path = self.root / 'runtime/servers' / str(self.port) / 'current.json'
        value = _read_json_retry(path, missing_ok=True)
        if value:
            value['supervisor_lock_exists'] = (path.parent / 'supervisor.lock').exists()
        return value

    def heartbeat(self):
        if self.remote:
            self.remote.execute(
                "sys.path.insert(0,str(root))\nfrom experiment_registry import heartbeat\nheartbeat(root,cfg['experiment_id'],cfg['owner_token'])\n",
                {'experiment_id': self.id, 'owner_token': self.token},
            )
        else:
            heartbeat(self.root, self.id, self.token)

    def start(self):
        """准备服务器实例和任务选择，启动对应后端并发布 ready 状态。"""
        self.record['status'] = 'allocating'
        self.save_record()
        arguments = [
            '--heap-mb',
            str(self.heap_mb),
            '--processors',
            str(self.processors),
            '--world-type',
            self.world_type,
        ]
        action = 'resume' if self._resume_instance else 'run'
        if self._resume_instance:
            arguments += ['--instance-id', self._resume_instance]
        spec = self.task_selection.spec
        if spec['mode'] == 'all':
            arguments += ['--task-list', 'all']
        elif spec['mode'] == 'difficulties':
            arguments += ['--task-list', *spec['difficulties']]
        else:
            arguments += ['--task-ids', *spec['task_ids']]
        self._admission_phase = 'admission'
        try:
            self.record['local_admission'] = admission(self.root, controller_mb=512)
            if self.task_worlds_enabled:
                self._admission_phase = 'lease'
                if self.remote:
                    self.remote.require_deployment()
                    self._lease_acquisition_unknown = True
                    self.remote.task_world_lease(
                        launch_id=self._launch_id, instance_id=self._resume_instance
                    )
                else:
                    self._lease_acquisition_unknown = True
                    with HostLease(self.root, timeout=5):
                        task_world_lease(self.root, self.port, self.id, self.token)
                self._task_world_lease = True
                self._lease_acquisition_unknown = False
            self._admission_phase = 'control'
            self._launch_dispatch_attempted = True
            if self.remote:
                arguments += ['--game-port', str(self.remote.settings['remote_game_port'])]
                if self._launch_id:
                    self.remote.control(action, arguments, launch_id=self._launch_id)
                else:
                    self.remote.control(action, arguments)
            else:
                log = (self.folder / 'launcher.log').open('ab')
                try:
                    self.server = subprocess.Popen(
                        [
                            sys.executable,
                            str(SOURCE_ROOT / 'tools/void_server.py'),
                            action,
                            '--port',
                            str(self.port),
                            '--experiment-id',
                            self.id,
                            *arguments,
                        ],
                        cwd=self.root,
                        env=self.env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                        start_new_session=os.name != 'nt',
                    )
                finally:
                    log.close()
            deadline = time.monotonic() + 210
            while time.monotonic() < deadline:
                state = self.state()
                if self.remote and state.get('experiment_id') != self.id:
                    failure = self.remote.execute(
                        "sys.path.insert(0,str(root))\nfrom experiment_registry import _read_json_retry\np=root/'runtime/experiments'/cfg['experiment_id']/'launch-error.json'\nprint(json.dumps(_read_json_retry(p,missing_ok=True)))\n",
                        {'experiment_id': self.id},
                    )
                    if json.loads(failure):
                        raise RuntimeError(
                            'Remote allocation failed: ' + json.loads(failure)['message']
                        )
                if state.get('experiment_id') == self.id:
                    if self._launch_id:
                        matched = self._confirm_startup_epoch(state)
                        if not matched:
                            if self.server is not None and self.server.poll() is not None:
                                raise RuntimeError(
                                    'Resume supervisor exited before startup epoch handshake'
                                )
                            time.sleep(1)
                            continue
                    self.allocated = True
                    if state['status'] == 'ready':
                        if (
                            self._resume_instance
                            and state.get('instance_id') != self._resume_instance
                        ):
                            raise RuntimeError('Explicit resume started a different world')
                        break
                    if state['status'] == 'stopped' and not self._resume_instance:
                        raise RuntimeError(
                            'Server startup failed; inspect ' + str(state.get('directory'))
                        )
                    self.heartbeat()
                elif state.get('status') in ('ready', 'starting'):
                    raise RuntimeError('Host busy: another experiment owns this server')
                if self.server is not None and self.server.poll() is not None:
                    raise RuntimeError('Local allocation failed; inspect launcher.log')
                time.sleep(1)
            else:
                raise TimeoutError('Experiment allocation timed out; inspect launcher state')
            if self.remote:
                with (self.folder / 'tunnel.log').open('ab') as log:
                    self.tunnel = subprocess.Popen(
                        self.remote.tunnel(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                        start_new_session=os.name != 'nt',
                    )
                for _ in range(50):
                    if self.tunnel.poll() is not None:
                        raise RuntimeError('Experiment tunnel failed')
                    try:
                        with socket.create_connection(('127.0.0.1', self.port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise TimeoutError('Experiment tunnel did not start')
            self.record.update(
                status='running',
                instance_id=state['instance_id'],
                world=state['world'],
                started_ns=time.time_ns(),
            )
            self._publish_ready_record()
            self.thread = threading.Thread(target=self._maintain, daemon=True)
            self.thread.start()
            return self
        except BaseException as exc:
            self.record['allocation_error'] = str(exc)
            self.record['start_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            capacity = False
            if isinstance(exc, CapacityUnavailable) and self._admission_phase in (
                'admission',
                'lease',
            ):
                self._lease_acquisition_unknown = False
                capacity = True
            elif isinstance(exc, RemotePrelaunchRejected):
                expected_action = (
                    'task_world_lease.acquire' if self._admission_phase == 'lease' else action
                )
                if exc.matches(
                    action=expected_action,
                    experiment_id=self.id,
                    instance_id=self._resume_instance,
                    launch_id=self._launch_id,
                ):
                    self.record['prelaunch_ack'] = exc.acknowledgement
                    capacity = exc.capacity_declined
                    if self._admission_phase == 'lease':
                        self._lease_acquisition_unknown = False
                    if self._admission_phase == 'control':
                        self._launch_dispatch_attempted = False
            self.record['capacity_declined'] = bool(
                capacity
                and not self._launch_dispatch_attempted
                and not self._lease_acquisition_unknown
            )
            self.record['launch_state'] = (
                'unknown' if self._launch_dispatch_attempted else 'not_started'
            )
            self.record['server_started'] = None if self._launch_dispatch_attempted else False
            self.finish('failed')
            raise

    def _maintain(self):
        next_heartbeat = 0
        while not self.stopped.wait(0.2):
            try:
                if time.monotonic() >= next_heartbeat:
                    self.heartbeat()
                    next_heartbeat = time.monotonic() + 5
                    sample = {
                        'wall_ns': time.time_ns(),
                        'host': memory(),
                        'manager': process_memory(os.getpid()),
                    }
                    if self.child and self.child.poll() is None:
                        sample['runner'] = process_memory(self.child.pid)
                    with (self.folder / 'local-memory.jsonl').open('a') as stream:
                        stream.write(json.dumps(sample) + '\n')
                if (
                    self.task_worlds
                    and not self._finishing
                    and (self._switch_thread is None or not self._switch_thread.is_alive())
                ):
                    for path in sorted(
                        (self.task_worlds.directory / 'requests').glob('*.json')
                    ):
                        if (self.task_worlds.directory / 'receipts' / path.name).exists():
                            continue
                        request = json.loads(path.read_text(encoding='utf-8'))
                        self._switch_thread = threading.Thread(
                            target=self._process_world_request, args=(request,), daemon=True
                        )
                        self._switch_thread.start()
                        break
            except Exception as exc:
                self.record['heartbeat_error'] = str(exc)
                self.save_record()

    def _process_world_request(self, request):
        try:
            self.task_worlds.process(request)
        except Exception as exc:
            # HostTaskWorlds retains pending/uncertain state and never replays it.
            self.record['task_world_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            self.save_record()
            rid = request.get('request_id', '')
            try:
                valid = str(uuid.UUID(rid)) == rid
            except (ValueError, TypeError, AttributeError):
                valid = False
            if valid:
                path = self.task_worlds.directory / 'receipts' / (rid + '.json')
                if not path.exists():
                    write(
                        path,
                        {
                            'request_id': rid,
                            'request': request,
                            'status': 'rejected',
                            'error': {'type': type(exc).__name__, 'message': str(exc)},
                        },
                    )

    def _stop_task_server(self):
        state = self.state()
        if state.get('experiment_id') != self.id:
            raise PermissionError('Refuse to switch a foreign world')
        if state.get('status') == 'ready':
            self.save(state)
            if self.remote:
                self.remote.control('stop')
            else:
                subprocess.run(
                    [
                        sys.executable,
                        str(SOURCE_ROOT / 'tools/void_server.py'),
                        'stop',
                        '--port',
                        str(self.port),
                    ],
                    env=self.env,
                    cwd=self.root,
                    check=True,
                    timeout=140,
                    capture_output=True,
                )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.state()
            if state.get('status') == 'stopped' and not state.get(
                'supervisor_lock_exists', False
            ):
                break
            time.sleep(0.2)
        if (
            state.get('status') != 'stopped'
            or state.get('process_alive', False)
            or state.get('supervisor_lock_exists', False)
            or state.get('exit_code') != 0
            or not state.get('shutdown_save_confirmed')
        ):
            raise RuntimeError(
                'Task world shutdown/save is not confirmed; no next world was started'
            )
        if self.server:
            self.server.wait(timeout=10)
            self.server = None
        return state

    def _switch_task_world(self, request):
        with self._switch_lock:
            if self._finishing:
                raise RuntimeError('Experiment is finishing; no task-world transition started')
            self.task_selection.require(request['task_id'])
            if request['operation'] == 'release':
                retained = request.get('retained') or {}
                state = self.state()
                if state.get('instance_id') != retained.get('instance_id'):
                    raise RuntimeError(
                        'Released physical world differs from active task workspace'
                    )
                state = self._stop_task_server()
                return {
                    'status': 'completed',
                    'instance_id': state['instance_id'],
                    'world': state['world'],
                    'save_confirmed': True,
                    'stopped_ns': state.get('stopped_ns'),
                    'outcome': request.get('outcome'),
                }
            state = self.state()
            retained = request.get('retained')
            if (
                retained
                and state.get('status') == 'ready'
                and state.get('instance_id') == retained.get('instance_id')
            ):
                authenticate(state, self.token)
                if (
                    state.get('owner_hash')
                    != hashlib.sha256(self.token.strip().encode()).hexdigest()
                    or state.get('experiment_id') != self.id
                    or state.get('world') != retained.get('world')
                ):
                    raise PermissionError('Ready retained world has a different owner/path')
                return {
                    'status': 'completed',
                    'instance_id': state['instance_id'],
                    'world': state['world'],
                    'fresh': False,
                    'resumed': True,
                }
            if state.get('status') != 'stopped':
                self._stop_task_server()  # discard only the owned bootstrap workspace
            arguments = [
                '--heap-mb',
                str(self.heap_mb),
                '--processors',
                str(self.processors),
                '--world-type',
                self.world_type,
                '--task-ids',
                request['task_id'],
            ]
            action = 'resume' if retained else 'run'
            if retained:
                arguments += ['--instance-id', retained['instance_id']]
            previous_id = state.get('instance_id')
            if self.remote:
                arguments += ['--game-port', str(self.remote.settings['remote_game_port'])]
                self.remote.control(action, arguments)
            else:
                log = (self.folder / 'launcher.log').open('ab')
                try:
                    self.server = subprocess.Popen(
                        [
                            sys.executable,
                            str(SOURCE_ROOT / 'tools/void_server.py'),
                            action,
                            '--port',
                            str(self.port),
                            '--experiment-id',
                            self.id,
                            *arguments,
                        ],
                        cwd=self.root,
                        env=self.env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                        start_new_session=os.name != 'nt',
                    )
                finally:
                    log.close()
            deadline = time.monotonic() + 210
            while time.monotonic() < deadline:
                current = self.state()
                if current.get('experiment_id') != self.id:
                    raise PermissionError(
                        'Physical server ownership changed during task activation'
                    )
                expected = (
                    current.get('instance_id') == retained['instance_id']
                    if retained
                    else current.get('instance_id') != previous_id
                )
                if expected and current.get('status') == 'ready':
                    break
                if self.server and self.server.poll() is not None:
                    raise RuntimeError('Task world launcher exited before ready')
                time.sleep(0.3)
            else:
                raise TimeoutError(
                    'Task world activation readiness uncertain; do not repeat launch'
                )
            self.record.update(instance_id=current['instance_id'], world=current['world'])
            self.save_record()
            return {
                'status': 'completed',
                'instance_id': current['instance_id'],
                'world': current['world'],
                'fresh': not bool(retained),
                'resumed': bool(retained),
            }

    def save(self, state):
        label = 'finish-' + str(uuid.uuid4())
        if self.remote:
            raw = self.remote.execute(
                "sys.path.insert(0,str(root))\nfrom experiment_registry import authenticate,write\n"
                "control=root/'runtime/servers'/str(cfg['remote_port'])\ns=json.loads((control/'current.json').read_text())\n"
                "authenticate(s,cfg['owner_token'])\n"
                "write(control/'save-request.json',{'instance_id':s['instance_id'],'action':'save','label':cfg['label']})\n"
                "deadline=time.monotonic()+60\nwhile time.monotonic()<deadline:\n"
                "    p=control/'save-confirmation.json'; ack=json.loads(p.read_text()) if p.exists() else {}\n"
                "    if ack.get('instance_id')==s['instance_id'] and ack.get('label')==cfg['label']: print(json.dumps(ack)); break\n"
                "    time.sleep(.2)\nelse: raise TimeoutError('Final save not confirmed')\n",
                {'label': label, 'owner_token': self.token},
                timeout=70,
            )
            return json.loads(raw)
        control = self.root / 'runtime/servers' / str(self.port)
        write(
            control / 'save-request.json',
            {'instance_id': state['instance_id'], 'action': 'save', 'label': label},
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            path = control / 'save-confirmation.json'
            ack = json.loads(path.read_text()) if path.exists() else {}
            if ack.get('instance_id') == state['instance_id'] and ack.get('label') == label:
                return ack
            time.sleep(0.2)
        raise TimeoutError('Final save not confirmed')

    def finish(self, outcome='completed'):
        """汇总运行状态，保存世界、停止实例并释放宿主资源。

        每一步的完成情况写入记录，后续调用据此继续收尾。
        """
        if getattr(self, '_finished', False):
            return self.record
        if self.record.get('status') in (
            'completed',
            'failed',
            'cancelled',
        ) and self.record.get('finished_ns'):
            return self.record
        if getattr(self, '_resume_staged', False):
            unpublished = self._finish_unpublished_resume(outcome)
            if unpublished is not None:
                return unpublished
        self._finishing = True
        if self._switch_thread and self._switch_thread.is_alive():
            self._switch_thread.join(timeout=480)
        if self._switch_thread and self._switch_thread.is_alive():
            self.record.update(
                status='cleanup_failed',
                cleanup_errors=[
                    'Task-world transition still in progress; host ownership retained'
                ],
            )
            self.save_record()
            return self.record
        errors = []
        state = {}
        saved = False
        released = False
        finalize_epoch = not getattr(self, '_resume_staged', False)
        if self.task_worlds and self.task_worlds.state.get('pending'):
            errors.append(
                'Unresolved task-world transition; inspect its atomic receipt before releasing host ownership'
            )
        self.record['requested_outcome'] = outcome
        self.record['status'] = 'finishing'
        self.save_record()
        try:
            state = self.state()
            if self._resume_staged:
                if not self._confirm_startup_epoch(state):
                    raise RuntimeError('Unconfirmed startup epoch during resume cleanup')
                finalize_epoch = True
            if state.get('experiment_id') == self.id:
                self.allocated = True
                if (
                    state.get('status') == 'stopped'
                    and state.get('exit_code') == 0
                    and state.get('shutdown_save_confirmed')
                ):
                    saved = True
                    self.record['final_save'] = {
                        'source': 'confirmed_clean_shutdown',
                        'stopped_ns': state.get('stopped_ns'),
                    }
                if state.get('stop_reason') == 'owner_heartbeat_expired':
                    outcome = 'failed'
                    self.record['owner_lease_expired'] = True
                if state.get('status') == 'ready':
                    try:
                        self.record['final_save'] = self.save(state)
                        saved = True
                    except Exception as exc:
                        errors.append('save: ' + str(exc))
                if state.get('status') in ('ready', 'starting', 'stopping'):
                    try:
                        if self.remote:
                            self.remote.control('stop')
                        else:
                            subprocess.run(
                                [
                                    sys.executable,
                                    str(SOURCE_ROOT / 'tools/void_server.py'),
                                    'stop',
                                    '--port',
                                    str(self.port),
                                ],
                                env=self.env,
                                cwd=self.root,
                                check=True,
                                timeout=140,
                                capture_output=True,
                            )
                    except Exception as exc:
                        errors.append('stop: ' + str(exc))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    state = self.state()
                    if state.get('status') == 'stopped' and not state.get(
                        'supervisor_lock_exists', False
                    ):
                        break
                    time.sleep(0.3)
                if self._resume_staged and not self._confirm_startup_epoch(state):
                    finalize_epoch = False
                    raise RuntimeError('Startup epoch changed during resume cleanup')
                released = (
                    state.get('status') == 'stopped'
                    and not state.get('process_alive', False)
                    and not state.get('supervisor_lock_exists', False)
                )
                self.record['world_retained'] = state.get('world')
            else:
                released = not self.allocated
        except Exception as exc:
            errors.append(str(exc))
        finally:
            self.stopped.set()
            if hasattr(self, 'thread'):
                self.thread.join(timeout=15)
            if self.tunnel:
                self.tunnel.terminate()
                try:
                    self.tunnel.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    errors.append('SSH tunnel did not exit')
            if self.server and released:
                try:
                    self.server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    errors.append('Local supervisor has not exited')
            save_not_required = None
            if released and not saved and self.allocated and not self.remote:
                try:
                    save_not_required = uninitialized_world_proof(
                        self.root,
                        self.port,
                        self.id,
                        self.token,
                        state,
                        task_world_state=self.task_worlds.state if self.task_worlds else None,
                    )
                    self.record['save_not_required'] = save_not_required
                except Exception as exc:
                    self.record['save_not_required_rejected'] = {
                        'type': type(exc).__name__,
                        'message': str(exc),
                    }
            clean = released and (saved or not self.allocated or save_not_required is not None)
            self.record.update(
                status=outcome if clean and not errors else 'cleanup_failed',
                finished_ns=time.time_ns(),
                resource_released=released,
                save_confirmed=saved,
                cleanup_errors=errors,
            )
            if self.task_worlds:
                self.record['task_worlds'] = self.task_worlds.state
            if self._task_world_lease and clean and not errors:
                try:
                    if self.remote:
                        self.remote.task_world_lease(release=True)
                    else:
                        with HostLease(self.root, timeout=5):
                            task_world_lease(
                                self.root, self.port, self.id, self.token, release=True
                            )
                    self._task_world_lease = False
                except Exception as exc:
                    self.record['status'] = 'cleanup_failed'
                    self.record['cleanup_errors'].append('task-world lease: ' + str(exc))
            self.save_record()
            self._finished = True
            if self.remote and finalize_epoch:
                try:
                    self.remote.execute(
                        "sys.path.insert(0,str(root))\nfrom experiment_registry import authenticate,write\np=root/'runtime/experiments'/cfg['experiment_id']/'experiment.json'\nif p.exists():\n    s=json.loads(p.read_text()); authenticate(s,cfg['owner_token'])\n    if cfg.get('launch_id') and s.get('launch_id')!=cfg['launch_id']: raise PermissionError('Refuse another startup epoch')\n    s.update(cfg['result']); write(p,s)\n",
                        {
                            'experiment_id': self.id,
                            'owner_token': self.token,
                            'launch_id': self._launch_id if self._resume_staged else None,
                            'result': {
                                k: self.record[k]
                                for k in (
                                    'status',
                                    'finished_ns',
                                    'resource_released',
                                    'save_confirmed',
                                    'cleanup_errors',
                                )
                            },
                        },
                    )
                except Exception as exc:
                    self.record['remote_finalize_error'] = str(exc)
                    self.record['status'] = 'cleanup_failed'
                    self.save_record()
            elif not self.remote and finalize_epoch:
                p = self.folder / 'experiment.json'
                if p.exists():
                    s = json.loads(p.read_text())
                    s.update(
                        {
                            k: self.record[k]
                            for k in (
                                'status',
                                'finished_ns',
                                'resource_released',
                                'save_confirmed',
                                'cleanup_errors',
                            )
                        }
                    )
                    write(p, s)
        return self.record

    def run(self, command, cwd=None):
        outcome = 'failed'

        def terminate(signum, frame):
            raise KeyboardInterrupt('Experiment cancelled')

        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, terminate)
        try:
            self.start()
            command = list(command)
            workdir = Path(cwd or self.root).resolve()
            candidate = workdir / command[0]
            if candidate.is_file():
                command[0] = str(candidate.resolve())
            self.child = subprocess.Popen(command, cwd=workdir, env=self.env)
            code = self.child.wait()
            outcome = 'completed' if code == 0 else 'failed'
            self.record['runner_exit_code'] = code
            return code
        except KeyboardInterrupt:
            outcome = 'cancelled'
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.record['runner_termination_unconfirmed'] = True
            raise
        finally:
            result = self.finish(outcome)
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
            if result['status'] == 'cleanup_failed':
                raise RuntimeError('Experiment cleanup incomplete: ' + str(self.record_path))

    def __enter__(self):
        return self.start()

    def environment(self, actor='ETwilight', **kwargs):
        if __package__:
            from .environment import Environment
            from .runtime_support import ActionSession
            from .adapters.bridge import Bridge
        else:
            from environment import Environment
            from runtime_support import ActionSession
            from adapters.bridge import Bridge
        bridge = Bridge(
            str(uuid.uuid4()), actor, 'server', port=self.port, token_file=self.token_file
        )
        if bridge.remote:
            bridge.remote.owner_token_file = self.token_file
            bridge.remote.experiment_id = self.id
        return Environment(
            actor,
            session=ActionSession(
                actor, run_id=bridge.run_id, server=bridge, client_required=False
            ),
            **kwargs
        )

    def __exit__(self, kind, value, trace):
        result = self.finish('completed' if kind is None else 'failed')
        if kind is None and result['status'] == 'cleanup_failed':
            raise RuntimeError('Experiment cleanup incomplete; inspect manager.json')
