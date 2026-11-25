"""Exchange durable task-world lifecycle requests and receipts between environment and
physical host.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

if __package__:
    from .experiment_registry import write
else:
    from experiment_registry import write


class TaskWorldError(RuntimeError):
    """Carry an incomplete or uncertain task-world transition's lifecycle evidence.

    receipt contains the supplied request/response record when available, or an
    empty dictionary. The exception message describes the transition error.

    Example:
        raise TaskWorldError('Task-world transition did not complete', receipt)

    The caller receives the lifecycle record needed to inspect or reconcile
    the same transition.
    """

    def __init__(self, message, receipt=None):
        """Set the transition error message and retain its optional receipt record."""

        super().__init__(message)
        self.receipt = receipt or {}


def world_key(task_id):
    """Validate a nonempty task ID and return its SHA-256 world-storage key."""

    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise ValueError('Invalid task world task_id')
    return hashlib.sha256(task_id.encode('utf-8')).hexdigest()


class TaskWorldManager:
    """Request retained task-world activation and release through the owning host.

    control_dir is Path | None, selected from configuration or the host's
    environment variable. backend accepts a lifecycle request and returns its
    receipt; the default backend writes requests/ and polls receipts/ in that
    directory. timeout and poll_seconds control receipt waiting.

    active is an activation receipt or None. worlds maps task IDs to retained
    activation receipts. pending is a request dict or None and is persisted
    before calling the backend. state_path stores these client-side fields in
    the environment session; _lock serializes lifecycle calls on this instance.
    Construction restores saved state and completed receipts. reconcile()
    waits for the pending request's existing receipt and accepts its result.

    Example:
        worlds = TaskWorldManager(environment, control_dir=control_dir)
        active = worlds.activate(task_id)
        released = worlds.release('task_passed')

    active identifies the task's retained physical world. released carries the
    host's completed save/release receipt, and the client clears its active binding.
    """

    def __init__(
        self, environment=None, *, backend=None, control_dir=None, timeout=480, poll_seconds=0.1
    ):
        """Bind the lifecycle backend and receipt polling, then restore persisted client and
        host state.
        """

        supplied = control_dir or os.environ.get('MC_TASK_WORLD_CONTROL_DIR')
        self.control_dir = Path(supplied) if supplied else None
        if backend is None and self.control_dir is None:
            raise TaskWorldError('Task worlds require a trusted task-world host')
        self.backend = backend or self._file_request
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.active = None
        # worlds: dict[str, dict], mapping task IDs to their retained activation receipts.
        self.worlds = {}
        # pending: dict | None, holding the exact lifecycle request awaiting an accepted
        # receipt.
        self.pending = None
        self._lock = threading.RLock()
        session = getattr(environment, 'session', None)
        self.state_path = (
            Path(session.directory) / 'task-worlds.json' if session is not None else None
        )
        self._restore()

    def _persist(self):
        """Write active, retained-world, and pending-request state when a session path is
        configured.
        """

        if self.state_path is not None:
            write(
                self.state_path,
                {'active': self.active, 'worlds': self.worlds, 'pending': self.pending},
            )

    def _restore(self):
        """Restore the saved bindings, accept matching completed receipts, and compare
        retained identities with the host.
        """

        previous = (
            json.loads(self.state_path.read_text(encoding='utf-8'))
            if self.state_path is not None and self.state_path.is_file()
            else None
        )
        host_path = self.control_dir / 'worlds.json' if self.control_dir is not None else None
        host = (
            json.loads(host_path.read_text(encoding='utf-8'))
            if host_path is not None and host_path.is_file()
            else None
        )
        state = previous or host or {}
        self.active = copy.deepcopy(state.get('active'))
        self.worlds = copy.deepcopy(state.get('worlds', {}))
        self.pending = copy.deepcopy(state.get('pending'))
        if self.pending and self.control_dir is not None:
            path = self.control_dir / 'receipts' / (self.pending['request_id'] + '.json')
            if path.exists():
                receipt = json.loads(path.read_text(encoding='utf-8'))
                if receipt.get('status') == 'completed':
                    self._accept(self.pending, receipt)
        if host is not None and self.pending is None:
            # Receipt publication and host-state publication use separate atomic writes.
            # Accept the completed receipt locally when host metadata still names its request as
            # pending.
            # The host commits that receipt's metadata when it processes the existing request.
            if host.get('pending'):
                pending = host['pending']
                path = self.control_dir / 'receipts' / (pending['request_id'] + '.json')
                receipt = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if receipt.get('status') != 'completed':
                    self.pending = copy.deepcopy(pending)
                else:
                    self.pending = copy.deepcopy(pending)
                    self._accept(pending, receipt)
            elif previous is not None and (host.get('active') or {}).get('instance_id') != (
                self.active or {}
            ).get('instance_id'):
                raise TaskWorldError(
                    'Restored environment and trusted host disagree about the active world; reconcile explicitly'
                )
            known = copy.deepcopy(host.get('worlds', {}))
            if host.get('pending'):
                pending = host['pending']
                path = self.control_dir / 'receipts' / (pending['request_id'] + '.json')
                receipt = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if receipt.get('status') == 'completed' and pending['operation'] == 'activate':
                    known[pending['task_id']] = receipt
            for task_id, world in self.worlds.items():
                actual = known.get(task_id, {})
                if (actual.get('instance_id'), actual.get('world')) != (
                    world.get('instance_id'),
                    world.get('world'),
                ):
                    raise TaskWorldError(
                        'Restored task world is absent or different in this trusted host; refusing a fresh substitute'
                    )
        self._persist()

    def status(self):
        """Return copied active, retained-world, and pending-request state."""

        with self._lock:
            return {
                'mode': 'per_task',
                'active_task_id': self.active['task_id'] if self.active else None,
                'active': copy.deepcopy(self.active),
                'worlds': copy.deepcopy(self.worlds),
                'pending': copy.deepcopy(self.pending),
            }

    def _file_request(self, request):
        """Write the identified lifecycle request when absent and wait for its host receipt."""
        # Use the control directory supplied by the owning experiment host.

        if not self.control_dir.is_dir():
            raise TaskWorldError('Trusted task-world control directory is unavailable')
        path = self.control_dir / 'requests' / (request['request_id'] + '.json')
        if not path.exists():
            write(path, request)
        return self._wait_receipt(request['request_id'])

    def _wait_receipt(self, request_id):
        """Poll this request's receipt until it appears or raise with an uncertain-state
        record at timeout.
        """

        path = self.control_dir / 'receipts' / (request_id + '.json')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if path.is_file():
                return json.loads(path.read_text(encoding='utf-8'))
            time.sleep(self.poll_seconds)
        raise TaskWorldError(
            'Task-world operation is uncertain; inspect the original request receipt',
            {'request_id': request_id, 'status': 'uncertain'},
        )

    def _accept(self, request, receipt):
        """Check a completed receipt, update the retained/active bindings, and clear the
        pending request.
        """

        if (
            receipt.get('request_id') != request['request_id']
            or receipt.get('status') != 'completed'
        ):
            raise TaskWorldError('Task-world transition did not complete', receipt)
        if request['operation'] == 'activate':
            for field in ('task_id', 'world_key', 'instance_id', 'world'):
                if not receipt.get(field):
                    raise TaskWorldError('Incomplete task-world activation receipt', receipt)
            if receipt['task_id'] != request['task_id'] or receipt['world_key'] != world_key(
                request['task_id']
            ):
                raise TaskWorldError('Task-world identity mismatch', receipt)
            old = self.worlds.get(request['task_id'])
            if old and (old['instance_id'], old['world']) != (
                receipt['instance_id'],
                receipt['world'],
            ):
                raise TaskWorldError('A revisit changed the retained world identity', receipt)
            self.active = copy.deepcopy(receipt)
            self.worlds[request['task_id']] = copy.deepcopy(receipt)
        else:
            if not receipt.get('save_confirmed'):
                raise TaskWorldError('Task world was not confirmed saved', receipt)
            self.active = None
        # pending: dict | None, holding the exact lifecycle request awaiting an accepted
        # receipt.
        self.pending = None
        self._persist()
        return copy.deepcopy(receipt)

    def _request(self, operation, **params):
        """Persist a newly identified pending request, call the backend once, and accept its
        receipt.
        """

        if self.pending is not None:
            raise TaskWorldError(
                'A task-world request remains unresolved; reconcile its receipt', self.pending
            )
        request = {'request_id': str(uuid.uuid4()), 'operation': operation, **params}
        self.pending = copy.deepcopy(request)
        self._persist()
        return self._accept(request, self.backend(copy.deepcopy(request)))

    def activate(self, task_id):
        """Return the matching active world or request activation of the selected task's
        retained workspace.
        """

        with self._lock:
            world_key(task_id)
            if self.active is not None:
                if self.active['task_id'] == task_id:
                    return copy.deepcopy(self.active)
                raise TaskWorldError(
                    'Release the active task workspace before selecting another task'
                )
            return self._request('activate', task_id=task_id)

    def release(self, outcome):
        """Request a confirmed save/release of the active task world, or return an
        already-inactive receipt.
        """

        with self._lock:
            if self.active is None:
                if self.pending:
                    raise TaskWorldError('Task-world request remains unresolved', self.pending)
                return {'status': 'completed', 'save_confirmed': True, 'already_inactive': True}
            return self._request(
                'release', task_id=self.active['task_id'], outcome=str(outcome)
            )

    def reconcile(self):
        """Accept the pending request's existing host receipt, or return status when no
        request is pending.
        """

        with self._lock:
            if not self.pending:
                return self.status()
            if self.control_dir is None:
                raise TaskWorldError(
                    'Injected backend needs explicit host reconciliation', self.pending
                )
            return self._accept(self.pending, self._wait_receipt(self.pending['request_id']))


class HostTaskWorlds:
    """Persist and dispatch physical task-world transitions for one owning host.

    directory contains worlds.json and per-request receipt files. backend is
    the physical-host callback accepting a request enriched with world_key and
    the copied retained-world record. state contains worlds (task ID -> receipt),
    active (receipt | None), and pending (request dict | None). lock serializes
    process() calls within this host object.

    process() records pending state before invoking the backend and writes a
    completed receipt before clearing pending. An existing receipt supplies
    repeated request IDs. Construction commits a completed pending receipt's
    metadata when a previous host process stopped between those writes.

    Example:
        host = HostTaskWorlds(directory, backend)
        receipt = host.process(
            {
                'request_id': str(uuid.uuid4()),
                'operation': 'activate',
                'task_id': task_id,
            }
        )

    receipt records the backend's activated world identity and the exact request.
    That same ID can retrieve its receipt after a caller reconnects.
    """

    def __init__(self, directory, backend):
        """Load host lifecycle state and finish any metadata commit backed by a completed
        receipt.
        """

        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / 'worlds.json'
        self.backend = backend
        self.lock = threading.RLock()
        self.state = (
            json.loads(self.path.read_text(encoding='utf-8'))
            if self.path.exists()
            else {'worlds': {}, 'active': None, 'pending': None}
        )
        pending = self.state.get('pending')
        if pending:
            completed = self.directory / 'receipts' / (pending['request_id'] + '.json')
            if (
                completed.exists()
                and json.loads(completed.read_text(encoding='utf-8')).get('status')
                == 'completed'
            ):
                self.process(pending)  # Commit metadata from the existing completed receipt.

    def process(self, request):
        """Retrieve a matching receipt or persist and execute one new physical transition,
        retaining uncertain outcomes.
        """

        with self.lock:
            rid = request['request_id']
            if str(uuid.UUID(rid)) != rid:
                raise ValueError('Invalid host task-world request ID')
            receipt_path = self.directory / 'receipts' / (rid + '.json')
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                if receipt.get('request') != request:
                    raise TaskWorldError(
                        'Task-world request ID was reused with different content'
                    )
                if (
                    self.state.get('pending') == request
                    and receipt.get('status') == 'completed'
                ):
                    task_id = request['task_id']
                    if request['operation'] == 'activate':
                        self.state['active'] = copy.deepcopy(receipt)
                        self.state['worlds'][task_id] = copy.deepcopy(receipt)
                    else:
                        self.state['active'] = None
                        self.state['worlds'][task_id]['release'] = copy.deepcopy(receipt)
                    self.state['pending'] = None
                    write(self.path, self.state)
                return receipt
            if self.state['pending']:
                raise TaskWorldError(
                    'Host task-world transition is unresolved; no operation was replayed',
                    self.state['pending'],
                )
            operation = request['operation']
            task_id = request['task_id']
            key = world_key(task_id)
            active = self.state['active']
            if operation not in ('activate', 'release'):
                raise ValueError('Unknown task-world operation')
            if operation == 'activate' and active is not None:
                raise TaskWorldError('Another task world is active')
            if operation == 'release' and (active is None or active['task_id'] != task_id):
                raise TaskWorldError('Task-world release does not match the active workspace')
            # Keep the exact request in pending state until its completed receipt and host
            # metadata are committed.
            self.state['pending'] = copy.deepcopy(request)
            write(self.path, self.state)
            try:
                retained = self.state['worlds'].get(task_id)
                result = self.backend(
                    {**request, 'world_key': key, 'retained': copy.deepcopy(retained)}
                )
                receipt = {
                    **copy.deepcopy(result),
                    'request_id': rid,
                    'request': copy.deepcopy(request),
                    'task_id': task_id,
                    'world_key': key,
                    'activation_id': rid,
                }
                if receipt.get('status') != 'completed':
                    raise TaskWorldError('Physical world transition did not complete', receipt)
                if operation == 'activate':
                    if not receipt.get('instance_id') or not receipt.get('world'):
                        raise TaskWorldError('Physical host returned no world identity')
                    if retained and (retained['instance_id'], retained['world']) != (
                        receipt['instance_id'],
                        receipt['world'],
                    ):
                        raise TaskWorldError(
                            'Physical host changed the retained task world identity'
                        )
                    self.state['active'] = copy.deepcopy(receipt)
                    self.state['worlds'][task_id] = copy.deepcopy(receipt)
                elif not receipt.get('save_confirmed'):
                    raise TaskWorldError('Physical host did not confirm world save')
                else:
                    self.state['active'] = None
                    self.state['worlds'][task_id]['release'] = copy.deepcopy(receipt)
                # Write the completed receipt before clearing the host's pending marker.
                # A restarted host can finish this metadata commit from that receipt.
                write(receipt_path, receipt)
                self.state['pending'] = None
                write(self.path, self.state)
                return receipt
            except Exception as exc:
                if receipt_path.exists():
                    completed = json.loads(receipt_path.read_text(encoding='utf-8'))
                    if completed.get('status') == 'completed':
                        # Keep the exact request in pending state until its completed receipt
                        # and host metadata are committed.
                        self.state['pending'] = copy.deepcopy(request)
                        raise TaskWorldError(
                            'Physical transition completed; metadata commit needs reconciliation',
                            completed,
                        ) from exc
                receipt = {
                    'request_id': rid,
                    'request': copy.deepcopy(request),
                    'status': 'uncertain',
                    'error': {'type': type(exc).__name__, 'message': str(exc)},
                }
                write(receipt_path, receipt)
                raise TaskWorldError(
                    'Host transition failed or is uncertain; inspect receipt before recovery',
                    receipt,
                ) from exc
