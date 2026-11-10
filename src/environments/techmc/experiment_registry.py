"""Per-port ownership, serialized admission and configurable host concurrency."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

if __package__:
    from .host_resources import CapacityUnavailable
else:
    from host_resources import CapacityUnavailable


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    payload = json.dumps(value, indent=2)
    temporary.write_text(payload, encoding='utf-8')
    for attempt in range(9):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 8:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.25))


def identifier(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid experiment ID')
    return value


def directory(root, experiment_id):
    return Path(root) / 'runtime/experiments' / identifier(experiment_id)


def credentials(root, experiment_id, token=None):
    path = directory(root, experiment_id) / 'owner.token'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text().strip()
        if token is not None and not secrets.compare_digest(existing, token):
            raise PermissionError('Experiment credential mismatch')
        token = existing
    else:
        token = token or secrets.token_hex(32)
        with path.open('x', encoding='utf-8') as stream:
            stream.write(token)
        path.chmod(0o600)
    return path, hashlib.sha256(token.encode()).hexdigest()


def authenticate(state, token):
    if state.get('owner_hash') and (
        not token
        or not secrets.compare_digest(
            state['owner_hash'], hashlib.sha256(token.strip().encode()).hexdigest()
        )
    ):
        raise PermissionError(
            'Server belongs to another experiment; supply its owner credential'
        )


def process_alive(pid):
    """OS verification; permission failures never mean that a process is dead."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('Missing recorded process identity')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return False  # invalid PID, not access denied
            raise OSError(error, 'Cannot verify recorded process termination')
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise OSError(ctypes.get_last_error(), 'Cannot read process termination')
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class HostLease:
    def __init__(self, root, port=None, timeout=0):
        self.root = Path(root)
        self.port = port
        self.timeout = timeout
        self.stream = None

    def __enter__(self):
        name = (
            'host.lock' if self.port is None else 'host-port-' + str(int(self.port)) + '.lock'
        )
        path = self.root / 'runtime' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open('a+b')
        self.stream.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt

                    if path.stat().st_size == 0:
                        self.stream.write(b'0')
                        self.stream.flush()
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                self.stream.close()
                self.stream = None
                raise RuntimeError(
                    'Host busy: another managed experiment holds this allocation/port lease'
                )
        return self

    def __exit__(self, *args):
        if self.stream:
            if os.name == 'nt':
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


def host_policy(root):
    path = Path(root) / 'runtime/host-policy.json'
    value = json.loads(path.read_text()) if path.exists() else {'max_servers': 0}
    maximum = value.get('max_servers', 0)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise ValueError('max_servers must be a nonnegative integer; 0 means resource-limited')
    return maximum


def allocations(root, exclude_port=None):
    """Include admitted-but-not-yet-started JVMs so concurrent starts cannot overbook."""
    root = Path(root)
    result = {}
    for path in (root / 'runtime/reservations').glob('*.json'):
        value = json.loads(path.read_text())
        port = int(value['port'])
        state_path = root / 'runtime/servers' / str(port) / 'current.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if (
            state.get('status') in ('stopped', 'deleted')
            and state.get('stopped_ns', 0) > value['created_ns']
            and not (state_path.parent / 'supervisor.lock').exists()
        ):
            continue
        result[port] = {
            **value,
            'pid': (
                state.get('pid')
                if state.get('status') in ('starting', 'ready', 'stopping', 'cleanup_failed')
                else None
            ),
        }
    for path in (root / 'runtime/servers').glob('*/current.json'):
        state = json.loads(path.read_text())
        port = int(path.parent.name)
        if port not in result and state.get('status') in (
            'starting',
            'ready',
            'stopping',
            'cleanup_failed',
        ):
            heap = state.get('heap_mb', 2048)
            result[port] = {
                'port': port,
                'pid': state.get('pid'),
                'game_port': state.get('game_port'),
                'memory_bytes': (heap + 384) * 1024**2,
                'disk_bytes': max(2048, heap + 1024) * 1024**2,
            }
    return [v for p, v in result.items() if p != exclude_port]


def _known_capacity_allocation(root, value):
    """Separate explicit live reservations from stale or damaged metadata."""
    pid = value.get('supervisor_pid')
    if pid is not None:
        return (
            type(pid) is int
            and pid > 0
            and type(value.get('created_ns')) is int
            and value['created_ns'] > 0
            and process_alive(pid)
        )
    state_path = Path(root) / 'runtime/servers' / str(int(value['port'])) / 'current.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    return (
        state.get('status') in ('starting', 'ready', 'stopping')
        and bool(state.get('experiment_id'))
        and bool(state.get('instance_id'))
        and isinstance(state.get('owner_hash'), str)
        and bool(re.fullmatch(r'[0-9a-f]{64}', state['owner_hash']))
    )


def check_existing(root, port, resume=False, max_servers=1, experiment_id=None):
    """Never treat an orphaned JVM, stale supervisor lock or failed cleanup as free."""
    owner_path = Path(root) / 'runtime/task-world-owners' / (str(int(port)) + '.json')
    if owner_path.exists():
        owner = json.loads(owner_path.read_text(encoding='utf-8'))
        if not experiment_id or owner.get('experiment_id') != experiment_id:
            if (
                owner.get('port') == int(port)
                and isinstance(owner.get('experiment_id'), str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', owner['experiment_id'])
                and isinstance(owner.get('owner_hash'), str)
                and re.fullmatch(r'[0-9a-f]{64}', owner['owner_hash'])
            ):
                raise CapacityUnavailable(
                    'Host busy: another experiment holds this allocation task-world lease'
                )
            raise RuntimeError('Task-world reservation identity is uncertain')
    for path in (Path(root) / 'runtime/servers').glob('*/current.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        active = value.get('status') in ('starting', 'ready', 'stopping')
        if value.get('status') == 'cleanup_failed':
            raise RuntimeError('Host busy or state uncertain on port ' + path.parent.name)
        if active and (int(path.parent.name) == int(port) or max_servers == 1):
            if (
                value.get('experiment_id')
                and value.get('experiment_id') != experiment_id
                and value.get('instance_id')
                and isinstance(value.get('owner_hash'), str)
                and re.fullmatch(r'[0-9a-f]{64}', value['owner_hash'])
            ):
                raise CapacityUnavailable(
                    'Host busy: another experiment actively occupies port ' + path.parent.name
                )
            raise RuntimeError('Host busy or state uncertain on port ' + path.parent.name)
        if active:
            continue
        if (path.parent / 'supervisor.lock').exists():
            raise RuntimeError('Supervisor lock needs reconciliation: ' + str(path.parent))
        pid = value.get('pid')
        if pid and os.name == 'nt' and process_alive(pid):
            raise RuntimeError(
                'Recorded process is still alive; refuse an unverified second allocation'
            )
        if pid and os.name != 'nt':
            cmdline = Path('/proc') / str(pid) / 'cmdline'
            try:
                alive = (
                    '-Dminecraft.agent.instance=' + value['instance_id']
                ).encode() in cmdline.read_bytes().split(b'\0')
            except FileNotFoundError:
                alive = False
            if alive:
                raise RuntimeError(
                    'Orphaned Minecraft JVM still running; refuse a second allocation'
                )
    current = allocations(root)
    if any(v['port'] == int(port) for v in current):
        if all(_known_capacity_allocation(root, v) for v in current if v['port'] == int(port)):
            raise CapacityUnavailable('Host busy: port is already admitted')
        raise RuntimeError('Port reservation is stale or its supervisor identity is uncertain')
    # A run reserves its slot even while all its task worlds are stopped. Count
    # physical allocations and whole-run leases once per port, never once per
    # receipt. The requesting owner's own slot is reused, not allocated twice.
    occupied_ports = {int(value['port']) for value in current}
    for path in (Path(root) / 'runtime/task-world-owners').glob('*.json'):
        owner = json.loads(path.read_text(encoding='utf-8'))
        reserved_port = int(path.stem)
        if (
            owner.get('port') != reserved_port
            or not owner.get('experiment_id')
            or not isinstance(owner.get('owner_hash'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', owner['owner_hash'])
        ):
            raise RuntimeError('Host busy: task-world reservation identity is uncertain')
        if reserved_port == int(port) and owner['experiment_id'] == experiment_id:
            continue
        occupied_ports.add(reserved_port)
    if max_servers and len(occupied_ports) >= max_servers:
        if any(not _known_capacity_allocation(root, v) for v in current):
            raise RuntimeError('Concurrency state includes an uncertain reservation')
        raise CapacityUnavailable('Host busy: configured server concurrency limit reached')


def task_world_lease(root, port, experiment_id, token, release=False):
    """Whole-run reservation survives stopped gaps; never discard crash remnants.

    Caller serializes admission with HostLease(root). Release is explicit and
    authenticated, and requires that this experiment has no live supervisor.
    """
    identifier(experiment_id)
    path = Path(root) / 'runtime/task-world-owners' / (str(int(port)) + '.json')
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if not release and previous.get('experiment_id') != experiment_id:
            if (
                previous.get('port') == int(port)
                and isinstance(previous.get('experiment_id'), str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', previous['experiment_id'])
                and isinstance(previous.get('owner_hash'), str)
                and re.fullmatch(r'[0-9a-f]{64}', previous['owner_hash'])
            ):
                raise CapacityUnavailable(
                    'Host busy: another experiment holds this allocation task-world lease'
                )
            raise RuntimeError('Task-world reservation identity is uncertain')
        authenticate(previous, token)
        if previous['experiment_id'] != experiment_id:
            raise PermissionError('Task-world lease belongs to another experiment')
    if release:
        state_path = Path(root) / 'runtime/servers' / str(int(port)) / 'current.json'
        state = (
            json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        )
        if (
            state.get('status') in ('ready', 'starting', 'stopping', 'cleanup_failed')
            or (state_path.parent / 'supervisor.lock').exists()
        ):
            raise RuntimeError(
                'Cannot release task-world lease while a server is active or uncertain'
            )
        path.unlink(missing_ok=True)
        return {'released': True}
    if not path.exists():
        check_existing(root, port, experiment_id=experiment_id, max_servers=host_policy(root))
        write(
            path,
            {
                'experiment_id': experiment_id,
                'port': int(port),
                'owner_hash': hashlib.sha256(token.strip().encode()).hexdigest(),
                'created_ns': time.time_ns(),
                'retention': 'explicit_authenticated_release',
            },
        )
    return {'released': False, 'experiment_id': experiment_id, 'port': int(port)}


def retained_instance(root, port, instance_id, experiment_id, token):
    """Validate an explicit stopped world without rewriting its historical receipt."""
    identifier(instance_id)
    original_base = Path(root) / 'runtime/servers' / str(int(port)) / 'instances'
    if any(
        p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
        for p in (original_base, original_base.parent)
    ):
        raise ValueError('Linked server instance storage is not permitted')
    base = original_base.resolve()
    target = base / instance_id
    if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
        raise ValueError('Linked retained world is not permitted')
    target = target.resolve()
    if target.parent != base:
        raise ValueError('Retained instance escapes this server port')
    value = json.loads((target / 'instance.json').read_text(encoding='utf-8'))
    authenticate(value, token)
    if value.get('experiment_id') != experiment_id:
        raise PermissionError('Retained world belongs to another experiment')
    if value.get('instance_id') != instance_id or Path(value['directory']).resolve() != target:
        raise ValueError('Retained instance identity does not match its directory')
    world = Path(value['world'])
    if (
        world.is_symlink()
        or (hasattr(world, 'is_junction') and world.is_junction())
        or world.resolve().parent != target
    ):
        raise ValueError('Retained world escapes its instance directory')
    if (
        value.get('status') != 'stopped'
        or value.get('exit_code') != 0
        or not value.get('shutdown_save_confirmed')
    ):
        raise RuntimeError(
            'Retained world lacks a confirmed clean shutdown; reconcile explicitly'
        )
    if not world.is_dir():
        raise FileNotFoundError('Retained world files are missing')
    return value


def retained_resume_target(root, port, instance_id, experiment_id, token):
    """Prove one owned saved world independently of the port's latest occupant.

    This is eligibility, not slot admission. The caller must still acquire the
    host/port leases and run check_existing before any launch. No current.json
    or other experiment's owner token is consulted or changed here.
    """
    value = retained_instance(root, port, instance_id, experiment_id, token)
    owner = hashlib.sha256(token.strip().encode()).hexdigest()
    if value.get('owner_hash') != owner:
        raise PermissionError('Retained resume target lacks exact owner identity')
    if value.get('process_alive'):
        raise RuntimeError('Retained process remains alive')
    for key in ('pid', 'supervisor_pid'):
        if process_alive(value.get(key)):
            raise RuntimeError('Retained process remains alive: ' + key)
    return value


def _read_json_retry(path, *, missing_ok=False):
    """Retry only a read of replaceable metadata; never retry authentication."""
    only_missing = True
    for attempt in range(9):
        try:
            value = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Experiment metadata must be an object')
            return value
        except (PermissionError, FileNotFoundError, ValueError, UnicodeError) as exc:
            only_missing = only_missing and isinstance(exc, FileNotFoundError)
            if attempt == 8:
                if missing_ok and only_missing:
                    return {}
                raise
            time.sleep(min(0.01 * 2**attempt, 0.25))


def heartbeat(root, experiment_id, token):
    path = directory(root, experiment_id)
    state = _read_json_retry(path / 'experiment.json')
    if state.get('experiment_id', experiment_id) != experiment_id:
        raise PermissionError('Heartbeat experiment identity mismatch')
    if not isinstance(state.get('owner_hash'), str) or not re.fullmatch(
        r'[0-9a-f]{64}', state['owner_hash']
    ):
        raise PermissionError('Heartbeat ownership metadata is missing or invalid')
    authenticate(state, token)
    if state.get('status') in ('completed', 'failed', 'cancelled', 'deleted'):
        raise RuntimeError('Experiment already finished')
    write(path / 'heartbeat.json', {'wall_ns': time.time_ns(), 'experiment_id': experiment_id})


_LEASE_OBSERVATIONS = {}


class _HeartbeatIdentityMismatch(ValueError):
    pass


def lease_expired(root, experiment_id, seconds):
    """A failed read is uncertainty, not a renewed lease or supervisor crash.

    Successful heartbeat timestamps retain a monotonic observation anchor. Read
    failures never move that anchor. With no prior successful read, inaccessible
    or corrupt data gets one bounded lease window; confirmed missing data does
    not manufacture a lease. Existing heartbeat files without explicit identity
    remain supported, scoped by their validated experiment directory.
    """
    path = directory(root, experiment_id) / 'heartbeat.json'
    key = (str(Path(root).resolve()), experiment_id)
    observed = _LEASE_OBSERVATIONS.setdefault(key, {})
    failure = None
    for attempt in range(9):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('Heartbeat must be an object')
            if 'experiment_id' in value and value['experiment_id'] != experiment_id:
                raise _HeartbeatIdentityMismatch('Heartbeat belongs to another experiment')
            stamp = value['wall_ns']
            if type(stamp) is not int or stamp < 0:
                raise ValueError('Invalid heartbeat timestamp')
            now_wall = time.time_ns()
            now_mono = time.monotonic()
            if stamp > now_wall:
                raise ValueError('Heartbeat timestamp is in the future')
            if observed.get('wall_ns') != stamp:
                observed.update(
                    wall_ns=stamp, observed_wall_ns=now_wall, observed_monotonic=now_mono
                )
            observed.pop('unverified_since', None)
            observed.pop('confirmed_missing', None)
            observed.pop('identity_mismatch', None)
            age = (
                (observed['observed_wall_ns'] - stamp) / 1_000_000_000
                + now_mono
                - observed['observed_monotonic']
            )
            return age > seconds
        except _HeartbeatIdentityMismatch:
            observed['identity_mismatch'] = True
            return True
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            failure = exc
            observed['last_read_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            if attempt < 8:
                time.sleep(min(0.01 * 2**attempt, 0.25))
    now = time.monotonic()
    if observed.get('identity_mismatch'):
        return True
    if 'wall_ns' in observed:
        age = (
            (observed['observed_wall_ns'] - observed['wall_ns']) / 1_000_000_000
            + now
            - observed['observed_monotonic']
        )
        return age > seconds
    if 'unverified_since' in observed:
        return now - observed['unverified_since'] > seconds
    if isinstance(failure, FileNotFoundError):
        observed['confirmed_missing'] = True
        return True
    if observed.get('confirmed_missing'):
        return True
    since = observed.setdefault('unverified_since', now)
    return now - since > seconds
