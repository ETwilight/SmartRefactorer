"""Generic isolated worker: resources and source pinning, no method branches."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from runtime.contracts import RunContext
from runtime.layout import RunLayout
from runtime.registry import resolve
from runtime.source_guard import SourceGuard, pin_source
from runtime.lifecycle import managed_environment


def source_files(roots):
    """Archive only declared dependencies, including individually shared helpers."""
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        for parent, children, files in os.walk(root):
            children[:] = [
                name
                for name in children
                if name
                not in (
                    '.venv',
                    'runs',
                    'launches',
                    'evidence',
                    'tests',
                    '__pycache__',
                    'node_modules',
                )
            ]
            for name in files:
                path = Path(parent) / name
                if path.suffix in ('.py', '.txt', '.js', '.json', '.md'):
                    yield path


def run(config, preflight, methods, source_directories):
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
        return _run_connected(
            config, layout, provider, environment, methods, source_directories
        )
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
                pass


def _run_connected(config, layout, provider, environment, methods, source_directories):
    """Run under the single connection owner's finally, including source archival."""
    options = {
        **config.get('options', {}),
        'auditor_factory': lambda folder: provider.auditor(folder, environment),
    }
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
    directories = source_directories
    for path in source_files(directories):
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
        report = resolve(methods, config['method']).run(context)
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
