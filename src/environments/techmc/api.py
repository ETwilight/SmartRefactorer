"""JSONL CLI for the exact same Environment.handle used by Python controllers."""

from pathlib import Path as _BootstrapPath
import sys as _BootstrapSys

if not __package__:
    _BootstrapSys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
    __package__ = "environments.techmc"
import argparse
import json
import os
import sys
from urllib.parse import urlsplit


def primitive():
    if __package__:
        from .adapters.bridge import Bridge, BridgeError
        from .environment import check_action
        from .actions.parameters import public_validation_action
    else:
        from adapters.bridge import Bridge, BridgeError
        from environment import check_action
        from actions.parameters import public_validation_action
    request = json.load(sys.stdin)
    side = request['side']
    check_action(
        public_validation_action(
            {'method': request['method'], 'side': side, 'params': request['params']}
        )
    )
    bridge = Bridge(
        request['run_id'],
        request['actor'],
        side,
        port=urlsplit(request['url']).port,
        timeout=140,
        token_file=request.get('token_file'),
    )
    try:
        value = {
            'status': 'ok',
            'result': bridge.call(
                request['method'], request['params'], request.get('request_id')
            ),
        }
    except Exception as exc:
        value = {'status': 'error', 'error': str(exc)}
        if isinstance(exc, BridgeError):
            value.update(native_feedback=exc.native_feedback, native_request=exc.native_request)
    print(json.dumps(value, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--actor', default='ETwilight')
    parser.add_argument('--run-id')
    parser.add_argument('--request-file')
    parser.add_argument('--stdio', action='store_true')
    parser.add_argument('--primitive', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--allow-execution-agent', action='store_true')
    parser.add_argument(
        '--port', type=int, default=int(os.environ.get('MC_AGENT_SERVER_PORT', '8776'))
    )
    parser.add_argument('--client-port', type=int, default=8765)
    if __package__:
        from .task_selection import add_selection_arguments, selection_from_args
    else:
        from task_selection import add_selection_arguments, selection_from_args
    add_selection_arguments(parser)
    args = parser.parse_args()
    if args.primitive:
        return primitive()
    if __package__:
        from .environment import Environment
    else:
        from environment import Environment
    environment = Environment(
        args.actor,
        args.run_id,
        allow_execution_agent=args.allow_execution_agent,
        server_port=args.port,
        client_port=args.client_port,
        task_selection=selection_from_args(args),
    )
    try:
        if args.request_file:
            from pathlib import Path

            requests = [Path(args.request_file).read_text(encoding='utf-8')]
        else:
            requests = sys.stdin
        for line in requests:
            try:
                response = environment.handle(json.loads(line))
            except Exception as exc:
                response = {
                    'protocol_version': '1.0',
                    'request_id': 'invalid',
                    'session_id': environment.session.run_id,
                    'status': 'error',
                    'result': None,
                    'error': {'type': type(exc).__name__, 'message': str(exc)},
                }
            print(json.dumps(response, ensure_ascii=True), flush=True)
            if environment.closed:
                break
    finally:
        environment.close()


if __name__ == '__main__':
    main()
