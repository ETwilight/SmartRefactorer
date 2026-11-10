"""Convenience CLI over the standard environment protocol; no policy or skills."""

from pathlib import Path as _BootstrapPath
import sys as _BootstrapSys

if not __package__:
    _BootstrapSys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
    __package__ = "environments.techmc"
import argparse
import json
import os
import uuid

if __package__:
    from .environment import Environment
    from .actions.catalog import action_sides as sides
else:
    from environment import Environment
    from actions.catalog import action_sides as sides
if __package__:
    from .task_selection import add_selection_arguments, selection_from_args
else:
    from task_selection import add_selection_arguments, selection_from_args
if __package__:
    from .contracts import OP_PARAMS
else:
    from contracts import OP_PARAMS


def main():
    p = argparse.ArgumentParser()
    p.add_argument('method')
    p.add_argument('--params', default='{}')
    p.add_argument('--side', choices=['client', 'server', 'local'])
    p.add_argument('--actor', default='ETwilight')
    p.add_argument('--run-id')
    p.add_argument('--request-id')
    p.add_argument(
        '--port', type=int, default=int(os.environ.get('MC_AGENT_SERVER_PORT', '8776'))
    )
    p.add_argument('--client-port', type=int, default=8765)
    add_selection_arguments(p)
    a = p.parse_args()
    environment = Environment(
        a.actor,
        a.run_id,
        server_port=a.port,
        client_port=a.client_port,
        task_selection=selection_from_args(a),
    )
    params = json.loads(a.params)
    # Dispatch public protocol operations from the authoritative contract. This
    # does not change their permissions or game requirements in Environment.
    service_ops = OP_PARAMS
    if a.method in service_ops:
        op = a.method
    else:
        op = 'action.execute'
        params = {
            'action': {
                'method': a.method,
                'side': a.side or sides(a.method)[0],
                'params': params,
            }
        }
    try:
        result = environment.handle(
            {
                'protocol_version': '1.0',
                'request_id': a.request_id or str(uuid.uuid4()),
                'op': op,
                'params': params,
            }
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result['status'] == 'error':
            raise SystemExit(1)
    finally:
        environment.close()


if __name__ == '__main__':
    main()
