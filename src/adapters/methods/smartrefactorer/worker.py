"""Prepare SR dependencies and provider settings, then run the shared method worker."""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from .registry import ENVIRONMENTS, METHODS, resolve
from runtime.worker import run as run_worker

RESEARCH_ROOT = Path(__file__).resolve().parents[3]


def preflight(config):
    """Check the configured provider, continuation state, and embedding dependencies."""

    if not os.environ.get('OPENAI_API_KEY'):
        raise ValueError('Model credential is unavailable')
    provider = resolve(ENVIRONMENTS, config['environment'])
    resolve(METHODS, config['method'])
    if hasattr(provider, 'preflight'):
        provider.preflight(config)
    resume_source = config.get('options', {}).get(config['method'] + '_resume_from')
    if resume_source is not None:
        from .checkpoint import load_continuation

        get_approval = getattr(provider, 'recovery_approval', None)
        approval = get_approval(config) if callable(get_approval) else None
        if approval is not None:
            raise ValueError(
                'SmartRefactorer does not support an unresolved-request bypass approval'
            )
        authority = getattr(provider, 'resume_readonly', None)
        state = load_continuation(
            resume_source, readonly=authority if callable(authority) else None
        )
        limits = {
            'total_tokens': config['token_budget'],
            'max_rounds': config.get('max_rounds'),
            'round_tokens': config.get('round_token_budget'),
        }
        original = {key: state['report']['limits'].get(key) for key in limits}
        if limits != original or config['goal'] != state['report']['goal']:
            raise ValueError('Continuation must retain the original goal and cumulative limits')
    if config['method'] == 'smartrefactorer':
        from . import policy
        from runtime import task_session as runner
        from algorithms.smartrefactorer import controller, library
        from algorithms.smartrefactorer.execution import python_backend

        for dependency in ('torch', 'transformers'):
            if importlib.util.find_spec(dependency) is None:
                raise ValueError('Missing dependency: ' + dependency)
        if config.get('options', {}).get('bge_device') == 'cuda:0':
            import torch

            if not torch.cuda.is_available():
                raise ValueError(
                    'Configured SR embedding GPU is unavailable in this interpreter'
                )
    return provider


def source_directories():
    """Return the SR, adapter, runtime, and audit directories recorded with this run."""
    roots = [
        RESEARCH_ROOT / name
        for name in (
            'algorithms/smartrefactorer',
            'adapters/methods/smartrefactorer',
            'adapters/environments/techmc',
            'runtime',
        )
    ]
    # Include runtime modules that are imported later when model and accounting work begins.

    roots.extend(
        RESEARCH_ROOT / name for name in ('runtime', 'adapters/environments/techmc', 'audit')
    )
    return list(dict.fromkeys(roots))


def run(config):
    """Run SR with its provider checks and recorded source directories."""

    return run_worker(config, preflight, METHODS, source_directories())


def main(argv=None):
    """Load worker configuration, check it or run it, and print the resulting run status."""

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.check:
        preflight(config)
        return 0
    report = run(config)
    print(json.dumps({'status': report['status'], 'run_dir': config['run_dir']}), flush=True)
    return 0


if __name__ == '__main__':
    from runtime.quiet_processes import enable_background_children

    enable_background_children()
    raise SystemExit(main())
