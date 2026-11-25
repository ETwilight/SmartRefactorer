"""Run the selected method worker through its environment provider's allocation lifecycle."""

import argparse
import json
import os
from pathlib import Path
import sys
from .method_worker import preflight

CAPACITY_EXIT = 75


def capacity_error(exc):
    """Check the allocation record for declined capacity before either server or method
    startup.
    """

    record = getattr(exc, 'allocation_record', {})
    return (
        record.get('capacity_declined') is True
        and record.get('launch_state') == 'not_started'
        and record.get('server_started') is False
        and record.get('method_started') is False
        and record.get('cleanup', {}).get('resource_released') is True
    )


def main(argv=None):
    """Preflight the selected method, allocate its worker, and save the resulting exit or
    capacity record.
    """

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    provider = preflight(
        config
    )  # Validate the configuration before requesting an environment allocation.
    run_dir = Path(config['run_dir'])
    run_dir.mkdir(parents=True, exist_ok=True)
    research = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        '-X',
        'utf8',
        '-m',
        'runtime.method_worker',
        '--config',
        str(args.config.resolve()),
    ]
    try:
        record = provider.run_allocated(config, command, research)
        code = record['worker_exit_code']
        (run_dir / 'allocation.json').write_text(
            json.dumps(record, indent=2) + '\n', encoding='utf-8'
        )
        return code
    except Exception as exc:
        record = dict(getattr(exc, 'allocation_record', {}) or {})
        record.update(
            status='waiting_for_capacity' if capacity_error(exc) else 'allocation_failed',
            error=record.get('error', str(exc)),
            port=config.get('port'),
        )
        (run_dir / 'allocation.json').write_text(
            json.dumps(record, indent=2) + '\n', encoding='utf-8'
        )
        if capacity_error(exc):
            return CAPACITY_EXIT
        raise


if __name__ == '__main__':
    raise SystemExit(main())
