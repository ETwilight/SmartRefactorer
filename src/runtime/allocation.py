"""Preflight a method, run its worker through the environment allocator, and save allocation
results.
"""

import argparse
import json
import os
import sys
from pathlib import Path

CAPACITY_EXIT = 75


def capacity_error(exc):
    """Recognize retryable capacity errors with records confirming the method never started
    and resources were released.
    """

    record = getattr(exc, 'allocation_record', {})
    # Requeue only allocations recording an unstarted method and released resources.
    # The error text then identifies the capacity condition.
    if record.get('method_started') is not False:
        return False
    accounting = record.get('method_accounting', {})
    if accounting.get('state') != 'not_started':
        return False
    if record.get('cleanup', {}).get('resource_released') is not True:
        return False
    text = str(exc).lower()
    return any(
        (
            s in text
            for s in (
                'host busy',
                'insufficient available memory',
                'insufficient disk',
                'already running',
                'port is already admitted',
                'concurrency limit',
                'another managed experiment',
                'holds this allocation',
            )
        )
    )


def main(argv=None, *, preflight, provider_for, worker_module, working_directory):
    """Read launch configuration, allocate the worker, and return its exit code or the
    capacity-wait code.
    """

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    preflight(config)
    run_dir = Path(config['run_dir'])
    run_dir.mkdir(parents=True, exist_ok=True)
    provider = provider_for(config['environment'])
    research = working_directory
    command = [
        sys.executable,
        '-X',
        'utf8',
        '-m',
        worker_module,
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
