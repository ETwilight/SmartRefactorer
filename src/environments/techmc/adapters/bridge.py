from __future__ import annotations
import json
import copy
import os
from pathlib import Path
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[3] / "assets/techmc"


def native_execution_state(feedback, request, side):
    """Only a matching terminal RPC envelope establishes callback completion."""
    if not isinstance(feedback, dict) or not isinstance(request, dict):
        return 'unknown'
    if feedback.get('source') != side or any(
        feedback.get(key) != request.get(key)
        for key in ('run_id', 'actor_id', 'request_id', 'method')
    ):
        return 'unknown'
    if not all(request.get(key) for key in ('run_id', 'actor_id', 'request_id', 'method')):
        return 'unknown'
    if 'fingerprint' in feedback:
        try:
            if json.loads(feedback['fingerprint']) != request:
                return 'unknown'
        except (ValueError, TypeError):
            return 'unknown'
    return 'completed' if feedback.get('status') in ('ok', 'error') else 'unknown'


class BridgeError(RuntimeError):
    def __init__(self, message, *, native_feedback=None, native_request=None, side=None):
        super().__init__(message)
        self.native_feedback = copy.deepcopy(native_feedback)
        self.native_request = copy.deepcopy(native_request)
        self.execution_state = native_execution_state(native_feedback, native_request, side)


class NativeExecutionError(BridgeError):
    """A returned native callback failed; its full frame is retained as evidence."""


class OperationNotStarted(ValueError):
    """Raised only around an explicit pre-dispatch validation boundary."""

    execution_state = 'not_started'

    def __init__(self, cause):
        super().__init__(str(cause))
        self.original_exception = cause


class Bridge:
    def __init__(self, run_id, actor_id, side='client', token_file=None, timeout=15, port=None):
        self.run_id, self.actor_id, self.timeout = run_id, actor_id, timeout
        self.side = side
        selected_port = '8765' if side == 'client' else '8776'
        port = int(
            port
            or os.environ.get(
                'MC_AGENT_CLIENT_PORT' if side == 'client' else 'MC_AGENT_SERVER_PORT',
                selected_port,
            )
        )
        if not 1 <= port <= 65535:
            raise ValueError('Invalid bridge port')
        self.url = 'http://127.0.0.1:' + str(port) + '/rpc'
        self.remote = None
        if side == 'server':
            if __package__ and '.' in __package__:
                from ..remote_host import for_port
            else:
                from remote_host import for_port
            self.remote = for_port(port, ROOT)
        experiment_token = (
            os.environ.get('MC_EXPERIMENT_TOKEN_FILE') if side == 'server' else None
        )
        self.token_file = Path(
            token_file
            or experiment_token
            or (self.remote.token_file if self.remote else ROOT / 'runtime/bridge.token')
        )

    def call(self, method, params=None, request_id=None):
        body = {
            'method': method,
            'params': params or {},
            'run_id': self.run_id,
            'actor_id': self.actor_id,
            'request_id': request_id or str(uuid.uuid4()),
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + self.token_file.read_text().strip(),
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            result = json.load(response)
        if result.get('status') != 'ok':
            raise BridgeError(
                json.dumps(result, ensure_ascii=False),
                native_feedback=result,
                native_request=body,
                side=self.side,
            )
        return result

    def batch(self, calls):
        results = []
        for call in calls:
            try:
                results.append(self.call(**call))
            except Exception as exc:
                results.append({'status': 'error', 'message': str(exc)})
                break
        return {'results': results, 'transactional': False}
