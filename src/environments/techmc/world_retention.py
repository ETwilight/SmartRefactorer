"""Explicit retention inventory. Pruning never deletes active or unconfirmed worlds."""

import json
from pathlib import Path
import shutil
import time

if __package__:
    from .experiment_registry import HostLease, check_existing, write, identifier
else:
    from experiment_registry import HostLease, check_existing, write, identifier


def inventory(root):
    root = Path(root)
    result = []
    for path in (root / 'runtime/servers').glob('*/instances/*/instance.json'):
        value = json.loads(path.read_text())
        directory = path.parent
        size = sum(
            p.stat().st_size for p in directory.rglob('*') if p.is_file() and not p.is_symlink()
        )
        experiment = (
            Path(root)
            / 'runtime/experiments'
            / str(value.get('experiment_id'))
            / 'experiment.json'
        )
        study = json.loads(experiment.read_text()) if experiment.is_file() else {}
        result.append(
            {
                k: value.get(k)
                for k in (
                    'instance_id',
                    'experiment_id',
                    'status',
                    'world',
                    'stopped_ns',
                    'exit_code',
                )
            }
            | {
                'directory': str(directory),
                'bytes': size,
                'experiment_status': study.get('status'),
                'retention': 'keep_until_explicit_prune',
            }
        )
    return result


def prune(root, instance_id, commit=False, include_failed=False):
    identifier(instance_id)
    root = Path(root).resolve()
    with HostLease(root):
        check_existing(root, 0)
        matches = [v for v in inventory(root) if v['instance_id'] == instance_id]
        if len(matches) != 1:
            raise ValueError('Expected one exact retained instance ID')
        value = matches[0]
        raw = Path(value['directory'])
        target = raw.resolve()
        if (
            raw.is_symlink()
            or not target.is_relative_to((root / 'runtime/servers').resolve())
            or target.name != instance_id
        ):
            raise ValueError('Invalid retained world path')
        if value['status'] != 'stopped':
            raise RuntimeError('Instance is not confirmed stopped')
        experiment = (
            root / 'runtime/experiments' / str(value.get('experiment_id')) / 'experiment.json'
        )
        experiment_state = (
            json.loads(experiment.read_text()).get('status') if experiment.is_file() else None
        )
        if not include_failed and (
            value.get('exit_code') != 0
            or experiment_state in ('failed', 'abandoned', 'interrupted', 'cleanup_failed')
        ):
            raise RuntimeError(
                'Failed world is protected; inspect its evidence and use --include-failed for explicit deletion'
            )
        if not commit:
            return {**value, 'action': 'would_delete'}
        current = target.parents[1] / 'current.json'
        state = json.loads(current.read_text()) if current.exists() else {}
        shutil.rmtree(target)
        if state.get('instance_id') == instance_id:
            state.update(status='deleted', deleted_ns=time.time_ns())
            write(current, state)
        receipt = root / 'runtime/deletions' / (instance_id + '.json')
        write(receipt, {**value, 'action': 'deleted', 'deleted_ns': time.time_ns()})
        if experiment.is_file():
            study = json.loads(experiment.read_text())
            study.setdefault('deleted_instances', []).append(instance_id)
            if study.get('instance_id') == instance_id:
                study['world_deleted_ns'] = time.time_ns()
            write(experiment, study)
        return {'instance_id': instance_id, 'action': 'deleted', 'receipt': str(receipt)}
