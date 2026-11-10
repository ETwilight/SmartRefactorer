"""Managed-server recovery. Never replay uncertain mutations or reset without a request."""

from .paths import SOURCE_ROOT
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import zipfile
import hashlib
from urllib.parse import urlsplit

if __package__:
    from .runtime_support import ROOT, atomic_json
    from .failure_contract import fault
    from .contracts import validate
else:
    from runtime_support import ROOT, atomic_json
    from failure_contract import fault
    from contracts import validate


def binding(environment):
    remote = getattr(environment.session.server, 'remote', None)
    if remote:
        return remote.state()
    url = getattr(environment.session.server, 'url', None)
    port = urlsplit(url).port if isinstance(url, str) else None
    if port is None:
        return {}
    path = getattr(environment, 'root', ROOT) / 'runtime/servers' / str(port) / 'current.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def available(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def checkpoint(environment, label):
    remote = getattr(environment.session.server, 'remote', None)
    if remote:
        state = remote.state(fresh=True)
        if state.get('status') != 'ready':
            return {'status': 'unavailable', 'world': state.get('world')}
        archive = environment.session.directory / 'world-checkpoints' / (label + '.zip')
        remote.checkpoint(label, archive)
        environment.session.event(
            'world.checkpoint_requested',
            label=label,
            world=state['world'],
            instance_id=state['instance_id'],
            remote=True,
        )
        return {
            'status': 'saved_files_archived',
            'world': state['world'],
            'remote': True,
            'archive': str(archive),
            'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'consistency': 'Remote confirmed save-all flush followed by saved-file copy; live ticks continue; not a process/actor snapshot',
        }
    state = binding(environment)
    if state.get('status') != 'ready':
        return {'status': 'unavailable', 'world': state.get('world')}
    port = state['bridge_port']
    atomic_json(
        getattr(environment, 'root', ROOT)
        / 'runtime/servers'
        / str(port)
        / 'save-request.json',
        {'instance_id': state['instance_id'], 'action': 'save', 'label': label},
    )
    environment.session.event(
        'world.checkpoint_requested',
        label=label,
        world=state['world'],
        instance_id=state['instance_id'],
    )
    confirmation = (
        getattr(environment, 'root', ROOT)
        / 'runtime/servers'
        / str(port)
        / 'save-confirmation.json'
    )
    deadline = time.monotonic() + 30
    confirmed = False
    while time.monotonic() < deadline:
        if confirmation.exists():
            ack = json.loads(confirmation.read_text(encoding='utf-8'))
            if ack.get('instance_id') == state['instance_id'] and ack.get('label') == label:
                confirmed = True
                break
        if not available(port):
            break
        time.sleep(0.2)
    if not confirmed:
        return {
            'status': 'save_unconfirmed',
            'world': state['world'],
            'note': 'No durability claim; retain audit evidence and original world',
        }
    archive = environment.session.directory / 'world-checkpoints' / (label + '.zip')
    archive.parent.mkdir(exist_ok=True)
    world = Path(state['world']).resolve()
    if not world.is_relative_to((ROOT / 'runtime/servers').resolve()):
        raise ValueError('World checkpoint path escaped managed runtime')
    # Saved-file snapshot, not a VM snapshot: ticks continue and transient actors are not checkpointed.
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as output:
        for path in world.rglob('*'):
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError('Linked world assets forbidden')
            if path.is_file() and path.name != 'session.lock':
                output.write(path, path.relative_to(world))
    return {
        'status': 'saved_files_archived',
        'world': str(world),
        'archive': str(archive),
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'consistency': 'Confirmed save-all flush followed by saved-file copy; live ticks continue; not a process/actor snapshot',
    }


def recover(environment, reset_world=False, reason='managed server unavailable'):
    previous = {}
    try:
        remote = getattr(environment.session.server, 'remote', None)
        if remote:
            remote.state(fresh=True)
        previous = binding(environment)
        result = _recover(environment, previous, reset_world, reason)
    except Exception as exc:
        result = {
            'status': (
                'ownership_mismatch' if isinstance(exc, PermissionError) else 'recovery_error'
            ),
            'recovered': False,
            'reset_available': False,
            'recovery_error': fault(exc),
            'note': 'Recovery did not establish readiness. Inspect state; do not replay uncertain operations.',
        }
        environment._auto_recovery_failed = True
        try:
            current = binding(environment)
            result.update(
                instance_id=current.get('instance_id'),
                world=current.get('world'),
                same_world=(
                    current.get('instance_id') == previous.get('instance_id')
                    if current and previous
                    else None
                ),
            )
            if (
                reset_world
                and current.get('instance_id')
                and current['instance_id'] != previous.get('instance_id')
            ):
                environment._stream_instances.clear()
        except Exception as secondary:
            result['state_error'] = fault(secondary)
    result.setdefault('recovered', False)
    result.setdefault('reset_available', False)
    result.setdefault('incident_id', str(uuid.uuid4()))
    result.setdefault('reason', reason)
    result.setdefault('stage', 'recovery')
    result.setdefault('old_instance_id', previous.get('instance_id'))
    result.setdefault('old_world', previous.get('world'))
    result.setdefault('passed_tasks', copy.deepcopy(getattr(environment, 'task_progress', {})))
    validate('server-recovery', result)
    events = getattr(environment, 'infrastructure_events', None)
    if events is None:
        environment.infrastructure_events = []
        events = environment.infrastructure_events
    events.append(copy.deepcopy(result))
    try:
        atomic_json(environment.session.directory / 'infrastructure.json', events)
    except Exception as exc:
        result['persistence_error'] = fault(exc)
        events[-1] = copy.deepcopy(result)
    return result


def _recover(environment, previous, reset_world, reason):
    remote = getattr(environment.session.server, 'remote', None)
    if not previous:
        return {'status': 'unmanaged', 'recovered': False, 'reset_available': False}
    expected = getattr(remote, 'experiment_id', None) or os.environ.get('MC_EXPERIMENT_ID')
    if expected and previous.get('experiment_id') and expected != previous['experiment_id']:
        raise PermissionError('Server belongs to another experiment')
    if previous.get('owner_hash'):
        if __package__:
            from .experiment_registry import authenticate
        else:
            from experiment_registry import authenticate
        authenticate(previous, Path(environment.session.server.token_file).read_text().strip())
    port = previous['bridge_port']
    # A listening SSH forward is not proof that its remote MC backend is alive.
    if available(port) and (
        not remote
        or (
            previous.get('status') == 'ready'
            and previous.get('process_alive', True)
            and previous.get('api_listening', True)
        )
    ):
        return {
            'status': 'already_available',
            'recovered': True,
            'world': previous['world'],
            'same_world': True,
            'note': 'No lifecycle change was performed. Reset requires confirmed shutdown.',
        }
    if previous.get('status') not in ('stopped',):
        return {
            'status': 'not_confirmed_stopped',
            'recovered': False,
            'reset_available': False,
            'note': 'A timeout alone never authorizes a second server or world reset',
        }
    action = 'run' if reset_world else 'resume'
    incident_id = str(uuid.uuid4())
    logs = environment.session.directory / 'recovery'
    logs.mkdir(exist_ok=True)
    command = [
        sys.executable,
        str(SOURCE_ROOT / 'tools/void_server.py'),
        action,
        '--port',
        str(port),
        '--game-port',
        str(previous['game_port']),
        '--heap-mb',
        str(previous.get('heap_mb', 3072)),
    ]
    if previous.get('experiment_id'):
        command += ['--experiment-id', previous['experiment_id']]
    if reset_world:
        selection = logs / (incident_id + '-selection.json')
        atomic_json(selection, previous['task_selection'])
        command += [
            '--world-type',
            previous.get('world_type', 'void'),
            '--task-selection-file',
            str(selection),
        ]
    # Original stopped world remains in its original instance directory in every path.
    if remote:
        arguments = [
            '--game-port',
            str(remote.settings['remote_game_port']),
            '--heap-mb',
            str(previous.get('heap_mb', 1024)),
            '--processors',
            str(previous.get('processors', 1)),
        ]
        if reset_world:
            # Selection is data, not a local Windows path passed to Linux.
            encoded = previous['task_selection']
            selection_name = (
                'remote-recovery-selection-' + str(remote.settings['remote_port']) + '.json'
            )
            remote.execute(
                "p=root/'runtime'/cfg['name']; p.write_text(json.dumps(cfg['selection']))\n",
                {'selection': encoded, 'name': selection_name},
            )
            arguments += [
                '--world-type',
                previous.get('world_type', 'void'),
                '--task-selection-file',
                remote.settings['remote_root'] + '/runtime/' + selection_name,
            ]
        output = remote.control(action, arguments)
        (logs / (incident_id + '.stdout.log')).write_text(output, encoding='utf-8')
        process = None
    else:
        with (logs / (incident_id + '.stdout.log')).open('w', encoding='utf-8') as out, (
            logs / (incident_id + '.stderr.log')
        ).open('w', encoding='utf-8') as err:
            variables = os.environ.copy()
            if previous.get('experiment_id'):
                variables['MC_EXPERIMENT_ID'] = previous['experiment_id']
                variables['MC_EXPERIMENT_TOKEN_FILE'] = str(
                    environment.session.server.token_file
                )
            process = subprocess.Popen(
                command,
                stdout=out,
                stderr=err,
                cwd=ROOT,
                env=variables,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
    deadline = time.monotonic() + 180
    current = previous
    while time.monotonic() < deadline:
        if remote:
            remote.state(fresh=True)
        current = binding(environment)
        if current.get('status') == 'ready' and available(port):
            break
        if process is not None and process.poll() is not None:
            break
        time.sleep(1 if remote else 0.5)
    same_world = (
        current.get('instance_id') == previous['instance_id']
        if current.get('instance_id')
        else None
    )
    identity_ok = same_world is (not reset_world)
    ok = current.get('status') == 'ready' and available(port) and identity_ok
    environment._auto_recovery_failed = not ok
    result = {
        'incident_id': incident_id,
        'reason': reason,
        'recovered': ok,
        'status': 'recovered' if ok else 'recovery_failed',
        'reset_available': not ok,
        'reset_op': (
            {'op': 'server.recover', 'params': {'reset_world': True}} if not ok else None
        ),
        'same_world': (
            current.get('instance_id') == previous['instance_id']
            if current.get('instance_id')
            else None
        ),
        'old_instance_id': previous['instance_id'],
        'instance_id': current.get('instance_id'),
        'old_world': previous['world'],
        'world': current.get('world'),
        'passed_tasks': copy.deepcopy(environment.task_progress),
        'uncertain_action': 'Not replayed. Inspect actual world state before issuing another mutation.',
        'clock_and_actors': 'In-progress acceptance is invalidated; fake-player state is not automatically reconstructed.',
    }
    if current.get('status') == 'ready' and not identity_ok:
        result['reset_available'] = False
        result['reset_op'] = None
        result['note'] = (
            'Ready server has an unexpected world identity; no successful resume/reset claim is made. Report to the host.'
        )
    if ok:
        environment.session.created_fakes.clear()
        environment.session.undo.clear()
        environment.session.initial_clock = None
        environment.session.clock_changed = False
    if reset_world and current.get('instance_id') != previous['instance_id']:
        environment._stream_instances.clear()
        result['task_instances'] = (
            'Start a new instance for unfinished tasks; completed pass records remain valid for their original worlds.'
        )
    return result


def after_request(environment, request, response):
    if response.get('status') == 'error' and response.get('execution', {}).get('state') in (
        'not_started',
        'completed',
    ):
        # A rejected request or a delivered terminal callback is not evidence of
        # a transport outage. Recovery must not erase this execution certainty.
        return None
    remote = getattr(
        getattr(getattr(environment, 'session', None), 'server', None), 'remote', None
    )
    if remote and response.get('status') in ('error', 'pending_or_unknown'):
        try:
            remote.state(fresh=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return {
                'status': 'server_or_transport_unavailable',
                'recovered': False,
                'reset_available': False,
                'note': 'SSH state unavailable; no server restart or action replay attempted.',
            }
    state = binding(environment)
    if not state:
        return None
    if state.get('status') != 'stopped':
        error = (response.get('error') or {}).get('type', '')
        unavailable = isinstance(response.get('result'), dict) and (
            response['result'].get('metrics', {}).get('infrastructure_failure')
            or any(
                c.get('id') == 'measurement_unavailable'
                for c in response['result'].get('checks', [])
            )
        )
        if (
            error
            in (
                'URLError',
                'TimeoutError',
                'TimeoutExpired',
                'ConnectionError',
                'ConnectionResetError',
                'RemoteDisconnected',
                'BridgeError',
            )
            or unavailable
        ):
            return {
                'status': 'server_or_transport_unavailable',
                'recovered': False,
                'process_state': state.get('status'),
                'reset_available': False,
                'note': 'A timeout is not proof of process death. Recovery waits for confirmed shutdown; no uncertain action was replayed.',
            }
        return None
    if not state.get('exit_code'):
        return None
    key = (state['instance_id'], state.get('stopped_ns'))
    attempted = getattr(environment, '_recovery_attempted', set())
    if key in attempted or getattr(environment, '_auto_recovery_failed', False):
        return {
            'status': 'server_unavailable',
            'reset_available': True,
            'reset_op': {'op': 'server.recover', 'params': {'reset_world': True}},
            'action_outcome': 'Unknown; do not blindly replay',
        }
    attempted.add(key)
    environment._recovery_attempted = attempted
    result = recover(
        environment, reason='Managed server crashed (exit code ' + str(state['exit_code']) + ')'
    )
    return result
