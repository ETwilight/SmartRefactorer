"""Trusted per-task world lifecycle; no model, task solving or game RPC here."""

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
    def __init__(self, message, receipt=None):
        super().__init__(message)
        self.receipt = receipt or {}


def world_key(task_id):
    if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
        raise ValueError('Invalid task world task_id')
    return hashlib.sha256(task_id.encode('utf-8')).hexdigest()


class TaskWorldManager:
    """Environment-facing client; the trusted parent performs physical switches.

    A timed-out request retains its exact ID and blocks further mutations. Calling
    reconcile() only reads its receipt; it never sends a second lifecycle request.
    """

    def __init__(
        self, environment=None, *, backend=None, control_dir=None, timeout=480, poll_seconds=0.1
    ):
        supplied = control_dir or os.environ.get('MC_TASK_WORLD_CONTROL_DIR')
        self.control_dir = Path(supplied) if supplied else None
        if backend is None and self.control_dir is None:
            raise TaskWorldError('Task worlds require a trusted task-world host')
        self.backend = backend or self._file_request
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.active = None
        self.worlds = {}
        self.pending = None
        self._lock = threading.RLock()
        session = getattr(environment, 'session', None)
        self.state_path = (
            Path(session.directory) / 'task-worlds.json' if session is not None else None
        )
        self._restore()

    def _persist(self):
        if self.state_path is not None:
            write(
                self.state_path,
                {'active': self.active, 'worlds': self.worlds, 'pending': self.pending},
            )

    def _restore(self):
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
            # A completed response and host metadata commit are separate atomic
            # writes. An old host pending record with our exact completed receipt
            # is recoverable by the host, but never authorizes a new transition.
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
        with self._lock:
            return {
                'mode': 'per_task',
                'active_task_id': self.active['task_id'] if self.active else None,
                'active': copy.deepcopy(self.active),
                'worlds': copy.deepcopy(self.worlds),
                'pending': copy.deepcopy(self.pending),
            }

    def _file_request(self, request):
        # This directory is supplied only by the authenticated experiment host.
        if not self.control_dir.is_dir():
            raise TaskWorldError('Trusted task-world control directory is unavailable')
        path = self.control_dir / 'requests' / (request['request_id'] + '.json')
        if not path.exists():
            write(path, request)
        return self._wait_receipt(request['request_id'])

    def _wait_receipt(self, request_id):
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
        self.pending = None
        self._persist()
        return copy.deepcopy(receipt)

    def _request(self, operation, **params):
        if self.pending is not None:
            raise TaskWorldError(
                'A task-world request remains unresolved; reconcile its receipt', self.pending
            )
        request = {'request_id': str(uuid.uuid4()), 'operation': operation, **params}
        self.pending = copy.deepcopy(request)
        self._persist()
        return self._accept(request, self.backend(copy.deepcopy(request)))

    def activate(self, task_id):
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
        with self._lock:
            if self.active is None:
                if self.pending:
                    raise TaskWorldError('Task-world request remains unresolved', self.pending)
                return {'status': 'completed', 'save_confirmed': True, 'already_inactive': True}
            return self._request(
                'release', task_id=self.active['task_id'], outcome=str(outcome)
            )

    def reconcile(self):
        with self._lock:
            if not self.pending:
                return self.status()
            if self.control_dir is None:
                raise TaskWorldError(
                    'Injected backend needs explicit host reconciliation', self.pending
                )
            return self._accept(self.pending, self._wait_receipt(self.pending['request_id']))


class HostTaskWorlds:
    """Durable host-side state machine. Backend is an owned physical host callback."""

    def __init__(self, directory, backend):
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
                self.process(pending)  # metadata commit only; never repeats physical I/O

    def process(self, request):
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
                # Persist the receipt before clearing pending. After a host crash,
                # an uncertain operation is never inferred safe to execute again.
                write(receipt_path, receipt)
                self.state['pending'] = None
                write(self.path, self.state)
                return receipt
            except Exception as exc:
                if receipt_path.exists():
                    completed = json.loads(receipt_path.read_text(encoding='utf-8'))
                    if completed.get('status') == 'completed':
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
