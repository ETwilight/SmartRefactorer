"""Run one independent experiment with admission, ownership and guaranteed cleanup attempts."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.techmc.experiment_manager import Experiment, ROOT
from environments.techmc.remote_host import for_port
from environments.techmc.world_retention import inventory, prune
from environments.techmc.task_selection import add_selection_arguments, selection_from_args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'worlds', 'prune'])
    parser.add_argument('--port', type=int, default=18776)
    parser.add_argument('--heap-mb', type=int, default=2048)
    parser.add_argument('--processors', type=int, default=1)
    parser.add_argument('--world-type', choices=['void', 'natural'], default='void')
    parser.add_argument('--instance-id')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--include-failed', action='store_true')
    parser.add_argument('--cwd', type=Path)
    add_selection_arguments(parser)
    # The separator keeps runner flags separate from host policy.
    values = sys.argv[1:]
    separator = values.index('--') if '--' in values else len(values)
    args = parser.parse_args(values[:separator])
    command = values[separator + 1 :]
    if args.action == 'run':
        if not command:
            parser.error('run requires -- followed by the research runner command')
        experiment = Experiment(
            args.port,
            args.heap_mb,
            args.processors,
            world_type=args.world_type,
            task_selection=selection_from_args(args),
        )
        try:
            code = experiment.run(command, args.cwd)
        finally:
            print(
                json.dumps(
                    {
                        'experiment_id': experiment.id,
                        'status': experiment.record['status'],
                        'record': str(experiment.folder / 'manager.json'),
                    },
                    indent=2,
                )
            )
        raise SystemExit(code)
    remote = for_port(args.port)
    if args.action == 'prune' and not args.instance_id:
        parser.error('prune requires --instance-id')
    if remote:
        code = "sys.path.insert(0,str(root))\nfrom world_retention import inventory,prune\nprint(json.dumps(inventory(root) if cfg['action']=='worlds' else prune(root,cfg['instance_id'],cfg['commit'],cfg['include_failed'])))\n"
        result = json.loads(
            remote.execute(
                code,
                {
                    'action': args.action,
                    'instance_id': args.instance_id,
                    'commit': args.commit,
                    'include_failed': args.include_failed,
                },
                timeout=180,
            )
        )
    else:
        result = (
            inventory(ROOT)
            if args.action == 'worlds'
            else prune(ROOT, args.instance_id, args.commit, args.include_failed)
        )
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
