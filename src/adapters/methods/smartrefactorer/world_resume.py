"""Method identity and historical initialization proof for environment recovery.

World/session/receipt verification belongs to adapters.environments.techmc.resume.
No recovery operation is performed by importing this module.
"""

import hashlib
from pathlib import Path
from environments.techmc.experiment_registry import _read_json_retry
from adapters.environments.techmc import resume as environment_resume


def _verify_plugin_import_recovery(path, source, root, original_manager):
    """Verify an exact pre-algorithm failed continuation; never accept flags as proof."""
    receipt = _read_json_retry(path)
    if receipt.get('schema') != 'smartrefactorer.plugin_import_recovery.v1':
        return None
    failed = Path(receipt['failed_run']).resolve()
    launch = _read_json_retry(failed / 'launch.json')
    error = _read_json_retry(failed / 'worker-error.json')
    if (
        launch.get('method') != 'smartrefactorer'
        or Path(launch.get('options', {}).get('smartrefactorer_resume_from', '')).resolve()
        != source
    ):
        raise ValueError('Initialization recovery is not a continuation of this source')
    if error != {'type': 'SyntaxError', 'message': 'invalid syntax (plugin.py, line 1)'}:
        raise ValueError('Recovery supports only the proven plugin import failure')
    for name in ('report.json', 'log/events.jsonl', 'events/events.jsonl'):
        p = failed / name
        if p.exists() and p.stat().st_size:
            raise ValueError(
                'Failed initialization has algorithm evidence; cannot use zero-work recovery'
            )
    current_path = (
        root / 'runtime/experiments' / original_manager['experiment_id'] / 'manager.json'
    )
    current = current_path.read_bytes()
    archived = (failed / 'environment/host/manager.json').read_bytes()
    manager = json.loads(archived)
    if current != archived:
        descendant = json.loads(current)
        parent = descendant.get('resume_from', {})
        if (
            parent.get('manager_sha256') != hashlib.sha256(archived).hexdigest()
            or parent.get('source_run') != str(source)
            or descendant.get('experiment_id') != manager['experiment_id']
            or descendant.get('status') not in ('allocating', 'running')
        ):
            raise ValueError(
                'Failed continuation host changed outside the authenticated descendant allocation'
            )
    if (
        not manager.get('save_confirmed')
        or not manager.get('resource_released')
        or manager.get('cleanup_errors')
        or manager.get('runner_exit_code') != 1
    ):
        raise ValueError('Failed initialization did not cleanly preserve the world')
    if (
        manager.get('resume_from', {}).get('source_run') != str(source)
        or manager.get('resume_from', {}).get('manager_sha256')
        != hashlib.sha256((source / 'environment/host/manager.json').read_bytes()).hexdigest()
    ):
        raise ValueError('Failed initialization host does not descend from this checkpoint')
    if (
        manager['experiment_id'] != original_manager['experiment_id']
        or manager['port'] != original_manager['port']
        or manager['task_selection'] != original_manager['task_selection']
    ):
        raise ValueError('Failed initialization changed host identity or task selection')
    return {
        'manager': manager,
        'manager_sha256': hashlib.sha256(archived).hexdigest(),
        'failed_run': str(failed),
    }


def uninvoked(source):
    state = _read_json_retry(Path(source) / 'method/state/smartrefactorer/development.json')
    return not any(
        state.get(key)
        for key in ('frames', 'pending', 'executed_candidate_hash', 'python_execution')
    )


def resume_descriptor(source, *, root, method='smartrefactorer', recovery_receipt=None):
    if method != 'smartrefactorer':
        raise ValueError('Explicit checkpoint belongs to a different method')
    return environment_resume.resume_descriptor(
        source,
        root=root,
        method=method,
        workflows=('smartrefactorer', 'smartrefactorer_compare'),
        uninvoked=uninvoked,
        initialization_recovery=_verify_plugin_import_recovery,
        recovery_receipt=recovery_receipt,
    )


def create_unstarted_recovery(source_run, failed_run, output_path, *, root):
    return environment_resume.create_unstarted_recovery(
        source_run, failed_run, output_path, root=root, descriptor_for=resume_descriptor
    )


verify_unstarted_recovery = environment_resume.verify_unstarted_recovery
