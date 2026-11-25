"""Track experiment credentials, server reservations, host/port locks, and owner heartbeats."""

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
    """Write JSON through a temporary file and retry destination replacement while a
    transient permission lock persists.
    """

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
    """Validate and return an experiment or instance identifier used in storage paths."""

    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid experiment ID')
    return value


def directory(root, experiment_id):
    """Return the experiment's directory under the configured TechMC runtime root."""

    return Path(root) / 'runtime/experiments' / identifier(experiment_id)


def credentials(root, experiment_id, token=None):
    """Load or create the experiment owner token and return its path and SHA-256 identity
    hash.
    """

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
    """Compare a recorded owner hash with the supplied token when that hash is present."""

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
    """Check a positive integer PID through the operating system, propagating errors that
    prevent verification.
    """
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
                return False  # ERROR_INVALID_PARAMETER identifies an invalid process ID.
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
    """Hold an operating-system file lock for host admission or one server port.

    root is the TechMC data Path. port is None for runtime/host.lock or a port
    selecting runtime/host-port-<port>.lock. timeout bounds lock-acquisition
    retries; zero makes one immediate attempt. stream is None before entry and
    after exit, and an open binary file while the lock is held.

    __enter__ acquires the byte/file lock and returns this lease. __exit__
    unlocks and closes its stream. Windows uses a one-byte msvcrt lock; POSIX
    uses flock on the opened file.

    Example:
        with HostLease(root, timeout=5):
            check_existing(root, port, max_servers=host_policy(root))

    The host admission check runs while this process holds the shared host
    lock, then the context releases it.
    """

    def __init__(self, root, port=None, timeout=0):
        """Select the host/port lock scope and acquisition timeout with no open lock stream."""

        self.root = Path(root)
        self.port = port
        self.timeout = timeout
        self.stream = None

    def __enter__(self):
        """Open and acquire the selected lock, polling within the configured timeout."""

        name = (
            'host.lock' if self.port is None else 'host-port-' + str(int(self.port)) + '.lock'
        )
        path = self.root / 'runtime' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # stream changes from None to the binary file that holds this operating-system lock.
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
        """Unlock the file and close its stream."""

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
    """Return the configured server-count cap; zero selects resource-limited scheduling."""

    path = Path(root) / 'runtime/host-policy.json'
    value = json.loads(path.read_text()) if path.exists() else {'max_servers': 0}
    maximum = value.get('max_servers', 0)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise ValueError('max_servers must be a nonnegative integer; 0 means resource-limited')
    return maximum


def allocations(root, exclude_port=None):
    """Collect per-port reservations and active-server allocations, including admitted
    launches without a PID.
    """
    root = Path(root)
    # result: dict[int, dict], merging reservations and active server records by bridge port.
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
    """Recognize an allocation through a live supervisor or its managed starting/active
    server metadata.
    """
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
    """Check the requested port and host concurrency against server state, supervisors,
    reservations, and whole-run leases.
    """
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
    # Count physical server allocations and whole-run leases by distinct port.
    # A whole-run lease retains its port during stopped gaps between task visits.
    # The requesting owner reuses its matching whole-run port lease.
    # occupied_ports: set[int], counting each server slot once across its allocation and
    # retained-run lease.
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
    """Create, reuse, or release the owner's whole-run port reservation under the caller's
    host lock.
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
    """Load an owned retained-instance receipt and check its stopped/saved state and
    world-directory binding.
    """
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
    """Check the retained world's exact owner hash and verify its recorded process and
    supervisor have exited.
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
    """Read a metadata object with up to nine attempts, optionally returning an empty dict
    when every attempt finds it missing.
    """
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
    """Authenticate the experiment identity and update its owner heartbeat timestamp."""

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


# _LEASE_OBSERVATIONS: dict[tuple[str, str], dict], keyed by resolved data root and experiment
# ID.
_LEASE_OBSERVATIONS = {}


class _HeartbeatIdentityMismatch(ValueError):
    """Identify a heartbeat file whose explicit experiment ID differs from the requested
    owner.

    lease_expired() catches this ValueError subclass, records identity_mismatch
    in the cached observation, and reports the lease expired.

    Example, after reading the heartbeat identity:
        raise _HeartbeatIdentityMismatch('Heartbeat belongs to another experiment')

    This distinguishes a different experiment's heartbeat from a transient
    metadata read error.
    """

    pass


def lease_expired(root, experiment_id, seconds):
    """Report whether the owner heartbeat has aged beyond the configured lease interval.

    Cache each new heartbeat timestamp with its wall-time age and a monotonic
    observation anchor. Repeated timestamps and failed reads age from that
    anchor. An initial unreadable/corrupt file starts one bounded observation
    window; confirmed absence or an explicit identity mismatch reports expiry.
    """
    path = directory(root, experiment_id) / 'heartbeat.json'
    key = (str(Path(root).resolve()), experiment_id)
    # observed is the shared mutable timing record retained across lease_expired() calls for
    # this owner.
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
                # A new timestamp resets the observation anchor; later elapsed time uses this
                # monotonic reference.
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
