"""Re-evaluate a recorded manifest against the same immutable task instance."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.techmc.environment import Environment
from environments.techmc.contracts import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('evaluation_id')
    parser.add_argument('--mode', choices=['auto', 'deterministic', 'hybrid'], default='auto')
    args = parser.parse_args()
    if not all(c.isalnum() or c in '-_' for c in args.evaluation_id):
        raise ValueError('Invalid evaluation ID')
    evidence = json.loads(
        (ROOT / 'runs' / args.evaluation_id / 'evidence.json').read_text(encoding='utf-8')
    )
    environment = Environment('ETwilight')
    try:
        result = environment.handle(
            {
                'protocol_version': '1.0',
                'request_id': str(uuid.uuid4()),
                'op': 'evaluation.run',
                'params': {'submission': evidence['submission'], 'mode': args.mode},
            }
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        if result['status'] != 'completed':
            raise SystemExit(1)
    finally:
        environment.close()


if __name__ == '__main__':
    main()
