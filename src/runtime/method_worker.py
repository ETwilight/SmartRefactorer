"""One isolated native method process, started only after host allocation succeeds."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

from .contracts import RunContext
from .layout import RunLayout
from .registry import ENVIRONMENTS, METHODS, resolve
from .source_guard import SourceGuard, pin_source
from .lifecycle import managed_environment


def preflight(config):
    if config['method'] == 'smartrefactorer':
        from adapters.methods.smartrefactorer.worker import preflight as sr_preflight

        return sr_preflight(config)
    if not os.environ.get('OPENAI_API_KEY'):
        raise ValueError('Model credential is unavailable')
    provider = resolve(ENVIRONMENTS, config['environment'])
    if hasattr(provider, 'preflight'):
        provider.preflight(config)
    resume_source = config.get('options', {}).get(config['method'] + '_resume_from')
    if resume_source is not None:
        if config['method'] == 'baseline':
            from runtime.continuation.baseline import load_continuation
        elif config['method'] == 'psn':
            from adapters.methods.psn.continuation.checkpoint import load_continuation
        else:
            raise ValueError('This method does not support explicit continuation')
        get_approval = getattr(provider, 'recovery_approval', None)
        approval = get_approval(config) if callable(get_approval) else None
        if approval is not None:
            if config['method'] != 'psn':
                raise ValueError(
                    'This method does not support approved unresolved-read continuation'
                )
            state = load_continuation(resume_source, recovery_approval=approval)
        else:
            state = load_continuation(resume_source)
        limits = {
            'total_tokens': config['token_budget'],
            'max_rounds': config.get('max_rounds'),
            'round_tokens': config.get('round_token_budget'),
        }
        original = {key: state['report']['limits'].get(key) for key in limits}
        if limits != original or config['goal'] != state['report']['goal']:
            raise ValueError('Continuation must retain the original goal and cumulative limits')
    if config['method'] == 'psn' and config.get('options', {}).get('psn_skill_library_dir'):
        if not (Path(config['options']['psn_skill_library_dir']) / 'skill_graph').is_dir():
            raise ValueError('PSN native library requires a skill_graph directory')
    if config['method'] == 'skillzip':
        options = config.get('options', {})
        for key in ('library_dir', 'bge_model_dir', 'bge_revision'):
            if not options.get(key):
                raise ValueError('SkillZip requires ' + key)
        for key in ('library_dir', 'bge_model_dir'):
            if not Path(options[key]).is_dir():
                raise ValueError('Missing ' + key)
        for name in ('torch', 'transformers'):
            if importlib.util.find_spec(name) is None:
                raise ValueError('Missing dependency: ' + name)
    return provider


def run(config):
    if config['method'] == 'smartrefactorer':
        from adapters.methods.smartrefactorer.worker import run as sr_run

        return sr_run(config)
    provider = preflight(config)
    layout = RunLayout(Path(config['run_dir'])).prepare()
    os.environ['EXPERIMENT_RUN_ID'] = layout.root.name
    os.environ['EXPERIMENT_OUTPUT_ROOT'] = str(layout.root.parent.parent)
    os.environ['EXPERIMENT_ENVIRONMENT'] = config['environment']
    connection_options = (
        provider.connection_options(config)
        if hasattr(provider, 'connection_options')
        else config.get('environment_options', {})
    )
    environment = managed_environment(provider.connect(connection_options))
    try:
        if hasattr(provider, 'configure_run'):
            provider.configure_run(environment, config)
        return _run_connected(config, layout, provider, environment)
    finally:
        try:
            environment.close()
        except Exception as exc:
            try:
                from audit.recorder import save_json

                save_json(
                    layout.root / 'cleanup-error.json',
                    {'type': type(exc).__name__, 'message': str(exc)},
                )
            except Exception:
                pass  # Diagnostic failure must not mask the primary error.


def _run_connected(config, layout, provider, environment):
    """Run under the single connection owner's finally, including source archival."""
    options = {
        **config.get('options', {}),
        'auditor_factory': lambda folder: provider.auditor(folder, environment),
    }
    # Config/launch files may contain only an untrusted receipt path. The
    # verified authorization is ephemeral and supplied by the provider afresh.
    options.pop('_resume_recovery_approval', None)
    get_approval = getattr(provider, 'recovery_approval', None)
    approval = get_approval(config) if callable(get_approval) else None
    context = RunContext(
        environment,
        layout,
        config['goal'],
        config['token_budget'],
        config.get('max_rounds'),
        config.get('round_token_budget'),
        options,
        recovery_approval=approval,
    )
    research = Path(__file__).resolve().parents[1]
    workspace = research.parent
    sources = {}
    paths = []
    directories = [
        research / name
        for name in (
            'runtime',
            'adapters/environments/techmc',
            'audit',
            'adapters/methods/' + config['method'],
            'algorithms/psn' if config['method'] == 'psn' else 'algorithms/baseline',
        )
    ]
    # Pin each deferred method's real sources, not just the plugin facade.
    if config['method'] in ('speedrunner', 'skillzip'):
        directories.append(
            research
            / (
                'algorithms/speedrunner'
                if config['method'] == 'speedrunner'
                else 'algorithms/skillzip'
            )
        )
    if config['method'] == 'psn':
        directories.append(research / 'adapters/methods/psn/prompts')
    for directory in directories:
        for parent, children, files in os.walk(directory):
            children[:] = [
                d
                for d in children
                if d
                not in (
                    '.venv',
                    'runs',
                    'launches',
                    'evidence',
                    'tests',
                    '__pycache__',
                    'node_modules',
                    '.python',
                    '.venvs',
                    '.python',
                    '.venvs',
                )
            ]
            for name in files:
                path = Path(parent) / name
                if path.suffix not in ('.py', '.txt', '.js', '.json', '.md'):
                    continue
                relative = path.relative_to(workspace)
                data = path.read_bytes()
                sources[relative.as_posix()] = hashlib.sha256(data).hexdigest()
                paths.append(path)
                target = layout.root / 'sources' / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    for path in getattr(environment, 'source_files', lambda: [])():
        relative = path.relative_to(workspace)
        data = path.read_bytes()
        sources[relative.as_posix()] = hashlib.sha256(data).hexdigest()
        paths.append(path)
        target = layout.root / 'sources' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (layout.root / 'runtime-source.json').write_text(
        json.dumps(sources, indent=2) + '\n', encoding='utf-8'
    )
    started = time.monotonic()
    with pin_source(SourceGuard(paths)):
        report = resolve(METHODS, config['method']).run(context)
    report['runtime'] = {
        'environment': config['environment'],
        'method': config['method'],
        'layout': layout.manifest(),
        'elapsed_seconds': time.monotonic() - started,
        'trial_round_limit': config.get('round_token_budget') is not None,
    }
    for name, digest in sources.items():
        try:
            changed = hashlib.sha256((workspace / name).read_bytes()).hexdigest() != digest
        except OSError:
            changed = True
        if changed:
            report['status'] = 'source_revision_changed'
            report.setdefault('runtime_source_changes', []).append(name)
    from audit.recorder import save_json

    save_json(layout.root / 'report.json', report)
    try:
        layout.finish(report)
    except Exception as exc:
        report.setdefault('projection_errors', []).append(
            {'stage': 'worker.layout', 'type': type(exc).__name__, 'message': str(exc)}
        )
        save_json(layout.root / 'report.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.check:
        preflight(config)
        print(json.dumps({'status': 'preflight_ok', 'method': config['method']}))
        return 0
    try:
        report = run(config)
        print(json.dumps({'status': report['status'], 'run_dir': config['run_dir']}))
        return (
            1
            if report['status']
            in (
                'failed',
                'source_revision_changed',
                'task_workspace_release_failed',
                'task_workspace_transition_uncertain',
                'accounting_blocked',
                'round_finalization_failed',
                'cleanup_failed',
            )
            else 0
        )
    except BaseException as exc:
        try:
            path = Path(config['run_dir'])
            path.mkdir(parents=True, exist_ok=True)
            (path / 'worker-error.json').write_text(
                json.dumps({'type': type(exc).__name__, 'message': str(exc)}, indent=2) + '\n',
                encoding='utf-8',
            )
        except Exception:
            pass  # A failed diagnostic must preserve the original control/error.
        raise


if __name__ == '__main__':
    raise SystemExit(main())
