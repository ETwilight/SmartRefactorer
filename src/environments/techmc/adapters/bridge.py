"""Send native client/server RPC requests and retain matching execution evidence on errors."""

from __future__ import annotations
import json
import copy
import os
from pathlib import Path
import urllib.request
import uuid

from environments.techmc.paths import DATA_ROOT as ROOT


def native_execution_state(feedback, request, side):
    """Return completed for a matching terminal RPC frame, otherwise unknown."""
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
    """Retain a failed native RPC response and its request identity.

    native_feedback and native_request are copied JSON values. execution_state
    is completed when the feedback matches the request, side, optional payload
    fingerprint, and terminal status; otherwise it is unknown. The exception
    message describes the failure seen by the caller.

    Example:
        raise BridgeError(
            message, native_feedback=frame, native_request=request, side='server'
        )

    The caller receives both the error and the native evidence used to classify
    whether its callback completed.
    """

    def __init__(self, message, *, native_feedback=None, native_request=None, side=None):
        """Copy the native request and feedback, then classify their correlated execution
        state.
        """

        super().__init__(message)
        self.native_feedback = copy.deepcopy(native_feedback)
        self.native_request = copy.deepcopy(native_request)
        self.execution_state = native_execution_state(native_feedback, native_request, side)


class NativeExecutionError(BridgeError):
    """Report failed native command callbacks within a returned RPC result.

    This uses BridgeError's copied native_request, native_feedback, and computed
    execution_state. ActionSession raises it when command callbacks fail, with
    the completed callback frame attached for its caller and invocation trace.

    Example:
        raise NativeExecutionError(
            message,
            native_feedback=result,
            native_request=request,
            side=side,
        )

    A matching terminal frame records completed execution with an unsuccessful
    command outcome. The caller can inspect that retained frame.
    """


class OperationNotStarted(ValueError):
    """Carry a preparation error with a not_started execution state.

    original_exception is the wrapped cause. Its message becomes this ValueError
    message, and execution_state is the class-level string not_started.
    Environment._validated() wraps failures from explicit preparation functions.

    Example, in a preparation exception handler:
        raise OperationNotStarted(exc) from exc

    The environment records the original error and the operation's pre-dispatch
    state in separate response fields.
    """

    execution_state = 'not_started'

    def __init__(self, cause):
        """Retain the preparation exception and copy its message."""

        super().__init__(str(cause))
        self.original_exception = cause


class Bridge:
    """Send authenticated native RPC requests for one run, actor, and bridge side.

    run_id and actor_id identify the request owner. side selects the client or
    server endpoint; url is its loopback HTTP RPC URL. token_file is the Path
    read for each request's bearer token. remote holds configured remote-server
    metadata when this server port has a remote mapping. timeout bounds each
    HTTP exchange. call() uses a supplied request ID or generates a UUID.

    Example:
        server = Bridge(run_id, actor, side='server', port=port)
        feedback = server.call('clock.query', request_id='clock-1')

    feedback is the native RPC envelope, with the clock data under result.
    batch() sends calls sequentially and returns the completed prefix plus the
    first error record when a call fails.
    """

    def __init__(self, run_id, actor_id, side='client', token_file=None, timeout=15, port=None):
        """Resolve the bridge port, remote mapping, RPC URL, and authentication-token path."""

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
        """Send one native request and return its ok envelope, raising BridgeError for a
        returned error status.
        """

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
            # result: decoded native RPC envelope dictionary, including its transport status and
            # operation result.
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
        """Run calls in order and stop after appending the first exception as an error
        record.
        """

        results = []
        for call in calls:
            try:
                results.append(self.call(**call))
            except Exception as exc:
                results.append({'status': 'error', 'message': str(exc)})
                break
        return {'results': results, 'transactional': False}
