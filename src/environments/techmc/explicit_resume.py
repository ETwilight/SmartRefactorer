"""Read-only verification for explicitly continuing a saved MC experiment.

This is a trusted launcher boundary, never an agent RPC or a fresh-world fallback.
"""

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from .experiment_registry import (
    _read_json_retry,
    authenticate,
    retained_instance,
    write,
    HostLease,
)
from .task_catalog import select_profile
from .task_selection import TaskSelection
from .task_stream import TaskStream


def resume_descriptor(source, *, root, method='baseline', recovery_receipt=None):
    source = Path(source)
    if not source.is_absolute():
        raise ValueError('Explicit resume requires an absolute run directory')
    source = source.resolve()
    root = Path(root).resolve()
    report = _read_json_retry(source / 'report.json')
    workflow = {'baseline': 'agent_baseline', 'psn': 'psn_compare'}.get(method)
    if workflow is None or report.get('workflow') != workflow:
        raise ValueError('Explicit checkpoint belongs to a different method')
    approval = None
    if report.get('status') == 'pending_request_unresolved':
        if recovery_receipt is None:
            raise ValueError(
                'Unresolved checkpoint requires explicit authenticated passive-wait recovery'
            )
        from .readonly_wait_recovery import verify_readonly_wait_recovery

        approval = verify_readonly_wait_recovery(source, recovery_receipt, root=root)
    if report.get('status') not in (
        'operator_stop',
        'infrastructure_interrupted',
        'model_context_blocked',
        'pending_request_unresolved',
    ):

        raise ValueError('Explicit resume requires a stopped checkpoint')
    manager_path = source / 'environment/host/manager.json'
    manager = _read_json_retry(manager_path)
    if (
        not manager.get('save_confirmed')
        or not manager.get('resource_released')
        or manager.get('cleanup_errors')
    ):
        raise ValueError('Baseline host has no confirmed clean saved release')
    archived = source / 'environment/session'
    with (archived / 'trace.jsonl').open(encoding='utf-8') as stream:
        first = json.loads(next(stream))
    session_id = first.get('run_id')
    reported = (
        report.get('discovery', {})
        .get('environment.describe', {})
        .get('feedback', {})
        .get('session_id')
    )
    if reported is not None and reported != session_id:
        raise ValueError('Reported session differs from canonical trace')
    if (
        not isinstance(session_id, str)
        or Path(session_id).name != session_id
        or session_id in ('.', '..')
    ):
        raise ValueError('Invalid original session identity')
    live = root / 'runs' / session_id
    for name in (
        'api-ledger.json',
        'environment-state.json',
        'task-stream.json',
        'task-worlds.json',
        'trace.jsonl',
    ):
        if (archived / name).read_bytes() != (live / name).read_bytes():
            raise ValueError('Original session changed after checkpoint: ' + name)
    state = _read_json_retry(live / 'environment-state.json')
    ledger = _read_json_retry(live / 'api-ledger.json')
    if any(
        e.get('response', {}).get('session_id', session_id) != session_id
        for e in ledger.values()
    ):
        raise ValueError('Foreign session feedback in checkpoint ledger')
    stream_state = _read_json_retry(live / 'task-stream.json')
    worlds = _read_json_retry(live / 'task-worlds.json')
    selection = TaskSelection(manager['task_selection'], world_type=manager['world_type'])
    manifest = stream_state['manifest']
    # Construction without a state path performs no writes.
    stream = TaskStream(
        selection,
        sequence=manifest['sequence'],
        task_ids=manifest['task_ids'] if manifest['sequence'] == 'custom' else None,
    )
    if stream.manifest != manifest:
        raise ValueError('Original task stream manifest changed')
    position = stream_state['position']
    history = stream_state['history']
    if (
        type(position) is not int
        or not isinstance(history, list)
        or position != len(history)
        or position < 0
    ):
        raise ValueError('Invalid original stream position')
    arrivals = list(manifest['task_ids'])
    for index, entry in enumerate(history):
        if (
            index >= len(arrivals)
            or entry.get('task_id') != arrivals[index]
            or entry.get('outcome') not in TaskStream.OUTCOMES
        ):
            raise ValueError('Original task stream history differs from arrivals')
        if entry['outcome'] == 'deferred':
            arrivals.append(entry['task_id'])
    if position >= len(arrivals):
        raise ValueError('Original task stream has no unfinished arrival')
    task_id = arrivals[position]
    candidates = []
    for entry in ledger.values():
        if (
            entry.get('request', {}).get('op') == 'task.start'
            and entry.get('response', {}).get('status') == 'completed'
        ):
            value = entry['response'].get('result', {})
            if (
                value.get('stream_position') == position
                and value.get('task', {}).get('id') == task_id
            ):
                candidates.append(value)
    if len(candidates) != 1:
        raise ValueError('Original visit has no unique task instance')
    value = candidates[0]
    instance = _read_json_retry(root / 'runs/task-instances' / (value['id'] + '.json'))
    for key in (
        'id',
        'actor',
        'server_bridge',
        'server_instance_id',
        'workspace_world_key',
        'workspace_world',
        'stream_position',
    ):
        if value.get(key) != instance.get(key):
            raise ValueError('Original task binding differs: ' + key)
    expected = select_profile(selection.get(task_id), value['task'].get('selected_profile'))
    if instance['task'] != expected:
        raise ValueError('Original task/profile definition changed')
    world = worlds.get('worlds', {}).get(task_id, {})
    active = worlds.get('active')
    if worlds.get('pending'):
        raise ValueError('Resume cannot bypass a pending task-world transition')
    if active:
        if (
            active.get('instance_id'),
            active.get('world_key'),
            active.get('task_id'),
            state.get('active_task_instance'),
            state.get('active_stream_position'),
        ) != (
            world.get('instance_id'),
            world.get('world_key'),
            task_id,
            instance['id'],
            position,
        ):
            raise ValueError(
                'Saved active logical workspace differs from checkpoint task binding'
            )
    elif state.get('active_task_instance') is not None:
        raise ValueError('Inactive saved workspace retains a contradictory task binding')
    if manager.get('task_worlds', {}).get('active') != active:
        raise ValueError('Saved host/client active workspace differs')
    if (world.get('instance_id'), world.get('world'), world.get('world_key')) != (
        instance['server_instance_id'],
        instance['workspace_world'],
        instance['workspace_world_key'],
    ):
        raise ValueError('Original world mapping differs from task binding')
    if (manager['instance_id'], manager['world']) != (world['instance_id'], world['world']):
        raise ValueError('Saved host is not the interrupted task world')
    if instance['id'] not in state.get('stream_instances', []):
        raise ValueError('Original instance is absent from session ownership')
    from .environment import Environment

    for entry in ledger.values():
        feedback = entry.get('response', {})
        relevant = entry.get('workspace_world_key') == world['world_key'] or (
            not entry.get('workspace_world_key')
            and entry.get('server_instance_id') == world['instance_id']
        )
        if (
            relevant
            and Environment._mutation_request(entry.get('request', {}))
            and (
                feedback.get('execution', {}).get('state') in ('unknown', 'running')
                or feedback.get('status') in ('pending_or_unknown', 'running')
            )
        ):
            from .readonly_wait_recovery import canonical_hash

            permitted = approval and any(
                item['request_id'] == entry.get('request', {}).get('request_id')
                and item['request_sha256'] == canonical_hash(entry.get('request', {}))
                and item['canonical_feedback_sha256'] == canonical_hash(feedback)
                for item in approval['retired_requests']
            )
            if not permitted:
                raise ValueError('Original session retains an unresolved request')
    port = urlsplit(instance['server_bridge']).port
    if (
        port != manager['port']
        or state['actor'] != instance['actor']
        or state['server_bridge'] != instance['server_bridge']
    ):
        raise ValueError('Original session/host identity mismatch')
    binding = {
        'verified': True,
        'task_id': task_id,
        'task_instance_id': instance['id'],
        'profile_id': instance['task'].get('selected_profile'),
        'server_instance_id': world['instance_id'],
        'workspace_world_key': world['world_key'],
        'stream_position': position,
        'session_id': session_id,
    }
    descriptor = {
        'source_run': str(source),
        'experiment_id': manager['experiment_id'],
        'manager_sha256': hashlib.sha256(manager_path.read_bytes()).hexdigest(),
        'manager': manager,
        'task_selection': manager['task_selection'],
        'actor': instance['actor'],
        'port': port,
        'session_id': session_id,
        'method': method,
        'binding': binding,
        'stream_sequence': manifest['sequence'],
        'stream_task_ids': manifest['task_ids'] if manifest['sequence'] == 'custom' else None,
    }
    if approval is not None:
        if any(
            approval['binding'].get(key) != value
            for key, value in binding.items()
            if key != 'verified'
        ):
            raise ValueError('Passive-wait approval binding differs from checkpoint')
        descriptor['recovery_approval'] = approval
    elif recovery_receipt is not None:
        receipt = verify_unstarted_recovery(recovery_receipt, descriptor, root=root)
        descriptor.update(
            live_manager_sha256=receipt['live_manager_sha256'],
            recovery_receipt=str(Path(recovery_receipt).resolve()),
            recovery=receipt,
        )
    return descriptor


def baseline_descriptor(source, *, root, recovery_receipt=None):
    return resume_descriptor(
        source, root=root, method='baseline', recovery_receipt=recovery_receipt
    )


def psn_descriptor(source, *, root, recovery_receipt=None):
    return resume_descriptor(source, root=root, method='psn', recovery_receipt=recovery_receipt)


def _signed_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode(
        'utf-8'
    )


def _owner(root, experiment_id):
    folder = Path(root) / 'runtime/experiments' / experiment_id
    token = (folder / 'owner.token').read_text(encoding='utf-8').strip()
    if not token:
        raise PermissionError('Missing original owner credential')
    return folder, token, hashlib.sha256(token.encode()).hexdigest()


def verify_unstarted_recovery(path, descriptor, *, root):
    receipt = _read_json_retry(path)
    body = {k: v for k, v in receipt.items() if k != 'owner_hmac_sha256'}
    _, token, owner = _owner(root, descriptor['experiment_id'])
    signature = hmac.new(token.encode(), _signed_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(receipt.get('owner_hmac_sha256', ''), signature):
        raise PermissionError('Unstarted recovery receipt authentication failed')
    if (
        body.get('schema'),
        body.get('experiment_id'),
        body.get('owner_hash'),
        body.get('source_run'),
        body.get('source_manager_sha256'),
        body.get('instance_id'),
    ) != (
        'explicit_unstarted_resume_recovery_v1',
        descriptor['experiment_id'],
        owner,
        descriptor['source_run'],
        descriptor['manager_sha256'],
        descriptor['binding']['server_instance_id'],
    ):
        raise ValueError('Unstarted recovery receipt belongs to a different checkpoint')
    allocation = Path(body['failed_run']) / 'allocation.json'
    if hashlib.sha256(allocation.read_bytes()).hexdigest() != body['allocation_sha256']:
        raise ValueError('Failed allocation evidence changed')
    snapshot = Path(body['failed_manager_snapshot'])
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != body['live_manager_sha256']:
        raise ValueError('Failed manager recovery snapshot changed')
    return receipt


def create_unstarted_recovery(source_run, failed_run, output_path, *, root):
    """Sign a new zero-paid recovery receipt; never edit historical documents.

    The world must already have been saved/stopped and its lease explicitly
    released by its owner. This function performs no physical lifecycle action.
    """
    root = Path(root).resolve()
    failed_run = Path(failed_run).resolve()
    output_path = Path(output_path).resolve()
    descriptor = baseline_descriptor(source_run, root=root)
    folder, token, owner = _owner(root, descriptor['experiment_id'])
    port = descriptor['port']
    from .experiment_manager import _process_alive

    with HostLease(root, timeout=5):
        allocation_path = failed_run / 'allocation.json'
        allocation = _read_json_retry(allocation_path)
        accounting = allocation.get('method_accounting', {})
        proof = accounting.get('proof', {})
        if (
            allocation.get('method_started') is not False
            or accounting.get('state') != 'not_started'
            or any(accounting.get(k) != 0 for k in ('charged_tokens', 'rounds', 'model_calls'))
            or not all(
                proof.get(k) is True
                for k in (
                    'host_child_is_none',
                    'runner_exit_code_absent',
                    'method_evidence_absent',
                )
            )
        ):
            raise ValueError('Failed attempt lacks explicit zero-paid unstarted proof')
        if Path(allocation.get('host_record', '')).resolve() != folder / 'manager.json':
            raise ValueError('Failed attempt belongs to a different host')
        if any(
            (failed_run / path).exists()
            for path in (
                'report.json',
                'log/events.jsonl',
                'events/events.jsonl',
                'accounting/summary.json',
            )
        ) or any(
            p.is_file()
            for name in ('accounting', 'method')
            for p in (failed_run / name).rglob('*')
        ):
            raise ValueError('Failed attempt contains method/accounting evidence')
        original = (folder / 'manager.json').read_bytes()
        manager = json.loads(original)
        if (
            manager.get('experiment_id') != descriptor['experiment_id']
            or manager.get('status') != 'cleanup_failed'
            or 'runner_exit_code' in manager
        ):
            raise ValueError('Only a failed unstarted manager can be reconciled')
        if manager.get('resume_from', {}).get('manager_sha256') != descriptor['manager_sha256']:
            raise ValueError('Failed manager does not descend from the original checkpoint')
        if manager.get('resume_from', {}).get('source_run') != descriptor['source_run']:
            raise ValueError('Failed manager source checkpoint differs')
        control = root / 'runtime/servers' / str(port)
        current = _read_json_retry(control / 'current.json')
        retained = retained_instance(
            root,
            port,
            descriptor['binding']['server_instance_id'],
            descriptor['experiment_id'],
            token,
        )
        authenticate(current, token)
        if (
            any(
                current.get(k) != retained.get(k)
                for k in (
                    'experiment_id',
                    'instance_id',
                    'directory',
                    'world',
                    'pid',
                    'supervisor_pid',
                    'status',
                    'stopped_ns',
                    'shutdown_save_confirmed',
                )
            )
            or current.get('owner_hash') != owner
            or retained.get('owner_hash') != owner
        ):
            raise PermissionError(
                'Recovered current/retained owner or shutdown evidence differs'
            )
        if current.get('process_alive') or any(
            _process_alive(current[k]) for k in ('pid', 'supervisor_pid') if current.get(k)
        ):
            raise ValueError('Recovered world still has a live process')
        for path in (
            control / 'supervisor.lock',
            root / 'runtime/reservations' / (str(port) + '.json'),
            root / 'runtime/task-world-owners' / (str(port) + '.json'),
        ):
            if path.exists():
                raise ValueError(
                    'Recovered world lock/reservation/lease must be explicitly released: '
                    + path.name
                )
        worlds = _read_json_retry(folder / 'task-world-control/worlds.json')
        if (
            worlds.get('pending')
            or worlds.get('active')
            or worlds != descriptor['manager'].get('task_worlds')
        ):
            raise ValueError(
                'Recovered task-world mapping changed or retains a pending transition'
            )
        live_sha = hashlib.sha256(original).hexdigest()
        snapshot = folder / 'resume-history' / ('manager-' + live_sha + '.json')
        snapshot.parent.mkdir(exist_ok=True)
        if snapshot.exists():
            if snapshot.read_bytes() != original:
                raise ValueError('Failed manager snapshot differs')
        else:
            with snapshot.open('xb') as stream:
                stream.write(original)
        body = {
            'schema': 'explicit_unstarted_resume_recovery_v1',
            'verified_ns': time.time_ns(),
            'experiment_id': descriptor['experiment_id'],
            'owner_hash': owner,
            'source_run': descriptor['source_run'],
            'source_manager_sha256': descriptor['manager_sha256'],
            'live_manager_sha256': live_sha,
            'failed_run': str(failed_run),
            'allocation_sha256': hashlib.sha256(allocation_path.read_bytes()).hexdigest(),
            'failed_manager_snapshot': str(snapshot),
            'instance_id': retained['instance_id'],
            'world': retained['world'],
            'current_sha256': hashlib.sha256(
                (control / 'current.json').read_bytes()
            ).hexdigest(),
            'instance_sha256': hashlib.sha256(
                (Path(retained['directory']) / 'instance.json').read_bytes()
            ).hexdigest(),
            'shutdown_save_confirmed': True,
            'stopped_ns': current['stopped_ns'],
            'method_started': False,
            'charged_tokens': 0,
            'model_calls': 0,
            'rounds': 0,
            'reason': 'failed_unstarted_reconciled',
        }
        if output_path.exists():
            raise FileExistsError('Recovery receipt already exists; preserve it')
        receipt = {
            **body,
            'owner_hmac_sha256': hmac.new(
                token.encode(), _signed_bytes(body), hashlib.sha256
            ).hexdigest(),
        }
        write(output_path, receipt)
        return receipt
