"""环境会话执行：给原生调用分配身份，保存事件并管理测量与清理。

ActionSession 将公共动作转换为 bridge 调用；同一调用的请求、反馈与执行状态
沿用同一个 request_id，供上层读取和恢复。
"""

from __future__ import annotations
import argparse
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import time
import uuid
import re
import threading

if __package__:
    from .adapters.bridge import Bridge, NativeExecutionError, native_execution_state
else:
    from adapters.bridge import Bridge, NativeExecutionError, native_execution_state

ROOT = Path(__file__).resolve().parents[2] / "assets/techmc"
PHASES = {'setup', 'build', 'warmup', 'measure', 'diagnose'}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    temp.write_text(payload, encoding='utf-8')
    # Retry only publication of the same complete bytes. Windows readers or
    # scanners can briefly lock the destination; no action is re-executed here.
    for attempt in range(9):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 8:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.25))


def production(before, after, ticks, seconds, warmup_ticks=0, mode='natural'):
    if ticks <= 0 or seconds <= 0:
        raise ValueError('Measurement requires positive actual game ticks and wall duration')
    delta = after - before
    if delta < 0:
        raise ValueError('Counter reset during measurement; result invalid')
    return {
        'raw_count': after,
        'delta_count': delta,
        'elapsed_game_ticks': ticks,
        'elapsed_wall_seconds': seconds,
        'warmup_ticks': warmup_ticks,
        'mode': mode,
        'per_game_hour': delta * 72000 / ticks,
        'per_wall_hour': delta * 3600 / seconds,
    }


def natural_wait(
    server, ticks, timeout_seconds=60, cancel=lambda: False, require_natural=False
):
    if ticks < 0 or timeout_seconds <= 0:
        raise ValueError('Invalid wait')
    first = server.call('clock.query')['result']
    start = time.monotonic()
    last = first

    def check_clock(clock):
        if require_natural and (clock['sprinting'] or clock['target_tps'] != 20):
            raise RuntimeError('Natural measurement requires no sprint and a 20 TPS target')

    check_clock(first)
    while last['tick'] - first['tick'] < ticks:
        if cancel():
            return {'status': 'cancelled', 'elapsed_game_ticks': last['tick'] - first['tick']}
        if last['frozen']:
            return {
                'status': 'stalled_frozen',
                'elapsed_game_ticks': last['tick'] - first['tick'],
            }
        if time.monotonic() - start > timeout_seconds:
            return {'status': 'timeout', 'elapsed_game_ticks': last['tick'] - first['tick']}
        time.sleep(0.05)
        last = server.call('clock.query')['result']
        check_clock(last)
    return {
        'status': 'completed',
        'elapsed_game_ticks': last['tick'] - first['tick'],
        'elapsed_wall_seconds': time.monotonic() - start,
        'initial': first,
        'final': last,
    }


class LocalExecutionError(RuntimeError):
    """A passive local operation returned a known unsuccessful terminal result.

    Correlation uses the same full envelope identity/fingerprint as RPC evidence;
    the source remains local and never claims that its wait condition succeeded.
    """

    def __init__(self, result, request):
        if not isinstance(request, dict) or request.get('method') != 'experiment.wait':
            raise ValueError('Local terminal evidence is restricted to passive experiment.wait')
        if not isinstance(result, dict) or result.get('status') not in (
            'timeout',
            'stalled_frozen',
            'cancelled',
        ):
            raise ValueError('Unsupported passive-wait terminal result')
        message = 'Passive wait ended without reaching its requested ticks: ' + result['status']
        super().__init__(message)
        self.local_request = copy.deepcopy(request)
        self.local_feedback = {
            **{key: request[key] for key in ('run_id', 'actor_id', 'request_id', 'method')},
            'source': 'local',
            'status': 'error',
            'result': copy.deepcopy(result),
            'error': message,
            'fingerprint': json.dumps(request, sort_keys=True, ensure_ascii=False),
        }

    def state_for(self, request):
        if not isinstance(request, dict) or self.local_request != request:
            return 'unknown'
        if not isinstance(self.local_feedback, dict):
            return 'unknown'
        result = self.local_feedback.get('result')
        if (
            self.local_feedback.get('status') != 'error'
            or not isinstance(result, dict)
            or result.get('status') not in ('timeout', 'stalled_frozen', 'cancelled')
        ):
            return 'unknown'
        return native_execution_state(self.local_feedback, request, 'local')

    @property
    def execution_state(self):
        return self.state_for(self.local_request)


class ActionSession:
    def __init__(
        self,
        actor,
        run_id=None,
        client=None,
        server=None,
        client_required=True,
        server_port=None,
        client_port=None,
        resume_existing=False,
    ):
        self.run_id = run_id or str(uuid.uuid4())
        self.actor = actor
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.run_id):
            raise ValueError('Invalid run ID')
        self.client = client or Bridge(self.run_id, actor, 'client', port=client_port)
        self.server = server or Bridge(self.run_id, actor, 'server', port=server_port)
        self.directory = ROOT / 'runs' / self.run_id
        existing_trace = self.directory / 'trace.jsonl'
        if existing_trace.exists() and not resume_existing:
            raise FileExistsError('Run already has evidence; use a new run ID')
        if resume_existing and (run_id is None or not existing_trace.is_file()):
            raise ValueError('Explicit session resume requires existing trace evidence')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = self.directory / 'trace.jsonl'
        self.sequence = 0
        self.created_fakes = set()
        self.undo = []
        self.initial_clock = None
        self.clock_changed = False
        self.preserve_clock = True
        self.keep_conditions = []
        self.client_settings = {}
        self.client_required = client_required
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._event_lock = threading.Lock()
        if resume_existing:
            # Only the append cursor is restored. Cleanup/heartbeat state belongs
            # to this newly connected process and must never replay old effects.
            with existing_trace.open(encoding='utf-8') as stream:
                for line in stream:
                    event = json.loads(line)
                    if event.get('run_id') != self.run_id:
                        raise ValueError('Foreign event in resumed session')
                    sequence = event.get('sequence')
                    if type(sequence) is not int or sequence <= self.sequence:
                        raise ValueError('Invalid resumed trace sequence')
                    self.sequence = sequence

    def event(self, kind, **data):
        with self._event_lock:
            self.sequence += 1
            value = {
                'sequence': self.sequence,
                'run_id': self.run_id,
                'kind': kind,
                'wall_ns': time.time_ns(),
                **data,
            }
            with self.events.open('a', encoding='utf-8') as f:
                f.write(json.dumps(value, ensure_ascii=False) + '\n')
            config = self.directory / 'trace-config.json'
            if config.exists():
                settings = json.loads(config.read_text(encoding='utf-8'))
                if settings['active'] and kind in settings['categories']:
                    with (self.directory / 'observations.jsonl').open(
                        'a', encoding='utf-8'
                    ) as f:
                        f.write(json.dumps(value, ensure_ascii=False) + '\n')

    def start_heartbeat(self):
        if self._heartbeat_thread is not None:
            return
        heartbeat_server = self.server

        def heartbeat():
            while not self._heartbeat_stop.wait(5):
                try:
                    heartbeat_server.call('session.heartbeat')
                except Exception as exc:
                    self.event('heartbeat.error', error=str(exc))

        self._heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def conditions(self, label):
        try:
            value = self.server.call('experiment.conditions')
        except Exception as exc:
            value = {'status': 'unavailable', 'reason': str(exc)}
        atomic_json(self.directory / (label + '-conditions.json'), value)
        self.event('experiment.conditions', label=label, result=value)

    def invoke(self, step, parent=None):
        """记录原生请求及其完成状态，再向调用方交付反馈。

        原生执行异常携带已有反馈；事件记录沿用原请求身份。
        """
        invocation = step.get('request_id') or str(uuid.uuid4())
        method = step['method']
        params = step.get('params', {})
        phase = step.get('phase', 'build')
        if phase not in PHASES:
            raise ValueError('Unknown experiment phase')
        # 原生调用与事件记录共用本次请求身份，后续反馈可以回到同一条执行记录。
        self.event(
            'invocation.start',
            invocation_id=invocation,
            parent_invocation_id=parent,
            skill_version=step.get('skill_version'),
            phase=phase,
            method=method,
            params=params,
        )
        try:
            if method == 'command.run' and not isinstance(params.get('text'), str):
                raise ValueError(
                    'command.run requires params.text (a command string); params.command is not supported'
                )
            if method == 'experiment.wait':
                result = natural_wait(self.server, **params)
                if result.get('status') in ('timeout', 'stalled_frozen', 'cancelled'):
                    raise LocalExecutionError(
                        result,
                        {
                            'method': method,
                            'params': copy.deepcopy(params),
                            'run_id': self.run_id,
                            'actor_id': self.actor,
                            'request_id': invocation,
                        },
                    )
                if result['status'] != 'completed':
                    raise RuntimeError(result)
            elif method == 'experiment.measure':
                result = self.measure(**params)
            elif method.startswith('skill.'):
                raise ValueError('Skills belong to external agents, not the environment')
            elif method.startswith(('wiki.', 'source.')):
                if __package__:
                    from .knowledge_backend import query
                else:
                    from knowledge_backend import query
                result = query(method, params)
            else:
                bridge = self.server if step.get('side') == 'server' else self.client
                if (
                    method == 'mod.config_set'
                    or (method.startswith('debug.') and method.endswith('.set'))
                ) and not params.get('keep_config', False):
                    key = (method, params.get('key'))
                    if key not in self.client_settings:
                        getter = (
                            method.replace('config_set', 'config_get')
                            if method == 'mod.config_set'
                            else method[:-3] + 'get'
                        )
                        old = self.client.call(
                            getter, {'key': params['key']} if 'key' in params else {}
                        )['result']
                        restore = {'enabled': old['enabled']}
                        if 'key' in params:
                            restore['key'] = params['key']
                        if method == 'debug.overlay.set' and params.get('entries'):
                            entries = self.client.call('debug.entries.list')['result']
                            restore['entries'] = {
                                name: entries[name]['status'] for name in params['entries']
                            }
                        self.client_settings[key] = restore
                        self.event('configuration.initial', method=method, params=restore)
                if method.endswith('fake_player.spawn'):
                    self.created_fakes.add(params['id'])
                changes_clock = method in (
                    'clock.freeze',
                    'clock.unfreeze',
                    'clock.step',
                    'clock.rate',
                    'clock.sprint',
                ) or (
                    method == 'command.run'
                    and re.search(
                        r'(?:^|run\s+)(?:minecraft:)?tick\s+(?!query)',
                        params.get('text', '').lstrip('/'),
                    )
                )
                if changes_clock:
                    if self.initial_clock is None:
                        self.initial_clock = self.server.call('clock.query')['result']
                    self.clock_changed = True
                result = bridge.call(method, params, request_id=invocation)
                if method.endswith('fake_player.spawn'):
                    self.created_fakes.add(params['id'])
                if method.endswith('fake_player.remove'):
                    self.created_fakes.discard(params['id'])
                if 'restore' in step:
                    self.undo.append(step['restore'])
                payload = result.get('result', {})
                if (
                    isinstance(payload, dict)
                    and payload.get('callbacks')
                    and not any(x['success'] for x in payload['callbacks'])
                ):
                    no_change = (
                        method == 'command.run'
                        and payload.get('effect') == 'no_change'
                        and payload.get('message_keys') == ['commands.fill.failed']
                        and payload.get('actual_changes') == 0
                    )
                    if not no_change:
                        raise NativeExecutionError(
                            f'Command failed: {payload}',
                            native_feedback=result,
                            native_request={
                                'method': method,
                                'params': params,
                                'request_id': invocation,
                                'run_id': self.run_id,
                                'actor_id': self.actor,
                            },
                            side=step.get('side'),
                        )
                if method.endswith('fake_player.spawn') and step.get('wait', True):
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        state = self.server.call('fake_player.status', {'id': params['id']})[
                            'result'
                        ]
                        if state['status'] == 'ready':
                            break
                        if state['status'] != 'spawning':
                            raise RuntimeError(f'Fake player spawn failed: {state}')
                        time.sleep(0.1)
                    else:
                        raise TimeoutError('Fake player spawn pending; cleanup will retire it')
                if (
                    isinstance(payload, dict)
                    and payload.get('action_id')
                    and step.get('wait', True)
                ):
                    action_id = payload['action_id']
                    deadline = time.monotonic() + 125
                    while time.monotonic() < deadline:
                        state = bridge.call('action.status', {'action_id': action_id})['result']
                        if state['status'] != 'running':
                            break
                        time.sleep(0.1)
                    else:
                        bridge.call('action.cancel', {'action_id': action_id})
                        raise TimeoutError('Action status timed out')
                    if state['status'] != 'completed':
                        raise RuntimeError(f'Action failed: {state}')
                    result['action_completion'] = state
            self.event(
                'invocation.end',
                invocation_id=invocation,
                parent_invocation_id=parent,
                status='completed',
                result=result,
            )
            return result
        except BaseException as e:
            try:
                self.event(
                    'invocation.end',
                    invocation_id=invocation,
                    parent_invocation_id=parent,
                    status='failed',
                    error=str(e),
                    **(
                        {
                            'native_feedback': copy.deepcopy(e.native_feedback),
                            'execution_state': e.execution_state,
                        }
                        if isinstance(e, NativeExecutionError)
                        else (
                            {
                                'local_feedback': copy.deepcopy(e.local_feedback),
                                'local_request': copy.deepcopy(e.local_request),
                                'execution_state': e.execution_state,
                            }
                            if isinstance(e, LocalExecutionError)
                            else {}
                        )
                    ),
                )
            except Exception as logging_error:
                note = 'Invocation trace persistence failed: ' + str(logging_error)
                if hasattr(e, 'add_note'):
                    e.add_note(note)
                else:
                    e.__notes__ = [*getattr(e, '__notes__', []), note]  # Python 3.10 runners.
            raise

    def measure(self, color, ticks, timeout_seconds=120, warmup_ticks=0, mode='natural'):
        if mode not in ('natural', 'step', 'sprint'):
            raise ValueError('Unknown measurement mode')
        if warmup_ticks:
            warm = natural_wait(self.server, warmup_ticks, timeout_seconds)
            if warm['status'] != 'completed':
                raise RuntimeError(warm)
        first_clock = self.server.call('clock.query')['result']
        before = self.server.call('counter.read', {'color': color})['result']
        start = time.monotonic()
        if mode == 'natural':
            wait = natural_wait(self.server, ticks, timeout_seconds)
        else:
            if mode == 'step' and not first_clock['frozen']:
                raise ValueError('step measurement requires explicitly frozen world')
            if self.initial_clock is None:
                self.initial_clock = first_clock
            self.clock_changed = True
            self.server.call('clock.' + mode, {'ticks': ticks})
            last = first_clock
            deadline = time.monotonic() + timeout_seconds
            while last['tick'] - first_clock['tick'] < ticks and time.monotonic() < deadline:
                time.sleep(0.05)
                last = self.server.call('clock.query')['result']
            wait = {
                'status': (
                    'completed' if last['tick'] - first_clock['tick'] >= ticks else 'timeout'
                )
            }
        after = self.server.call('counter.read', {'color': color})['result']
        end = time.monotonic()
        last_clock = self.server.call('clock.query')['result']
        if wait['status'] != 'completed':
            raise RuntimeError(wait)
        actual_mode = (
            'sprint'
            if mode == 'sprint' or first_clock['sprinting'] or last_clock['sprinting']
            else mode
        )
        seconds = (
            (after['sample_monotonic_ns'] - before['sample_monotonic_ns']) / 1e9
            if 'sample_monotonic_ns' in before and 'sample_monotonic_ns' in after
            else end - start
        )
        result = production(
            before['total'],
            after['total'],
            after['tick'] - before['tick'],
            seconds,
            warmup_ticks,
            actual_mode,
        )
        result.update(
            {
                'color': color,
                'before': before,
                'after': after,
                'initial_clock': first_clock,
                'final_clock': last_clock,
                'items_consumed_by_counter': True,
            }
        )
        atomic_json(self.directory / f'measurement-{uuid.uuid4()}.json', result)
        return result

    def cleanup(self):
        errors = []
        if self.client_required:
            try:
                self.client.call('action.cancel')
            except Exception as e:
                errors.append(str(e))
        for name in sorted(self.created_fakes):
            for operation in ('stop', 'remove'):
                try:
                    self.server.call('fake_player.' + operation, {'id': name})
                except Exception as e:
                    errors.append(f'{name}.{operation}: {e}')
        for step in reversed(self.undo):
            try:
                self.invoke(step)
            except Exception as e:
                errors.append(str(e))
        for (method, key), params in reversed(list(self.client_settings.items())):
            try:
                self.client.call(method, params)
            except Exception as e:
                errors.append('client setting restoration: ' + str(e))
        if self.clock_changed and self.initial_clock and self.preserve_clock:
            for text in (
                'tick sprint stop',
                'tick step stop',
                'tick rate ' + str(self.initial_clock['target_tps']),
                'tick freeze' if self.initial_clock['frozen'] else 'tick unfreeze',
            ):
                try:
                    self.server.call('command.run', {'text': text})
                except Exception as e:
                    errors.append('clock restoration: ' + str(e))
        try:
            restored = self.server.call('experiment.restore', {'keep': self.keep_conditions})[
                'result'
            ]
            errors.extend(restored.get('errors', []))
            self.event('environment.restore', result=restored)
        except Exception as e:
            errors.append('environment restoration: ' + str(e))
        try:
            self.conditions('final')
        except Exception as e:
            errors.append('final conditions: ' + str(e))
        try:
            self.event('cleanup', errors=errors)
        except Exception as e:
            errors.append('cleanup trace: ' + str(e))
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=1)
        try:
            atomic_json(
                self.directory / 'cleanup.json', {'errors': errors, 'complete': not errors}
            )
        except Exception as e:
            errors.append('cleanup persistence: ' + str(e))
        return errors

    @staticmethod
    def predicate(predicate, results):
        value = results[predicate['step']]
        for key in predicate['path'].split('.'):
            value = value[int(key)] if isinstance(value, list) else value[key]
        expected = predicate['value']
        op = predicate.get('op', 'eq')
        if op == 'eq':
            return value == expected
        if op == 'gte':
            return value >= expected
        if op == 'lte':
            return value <= expected
        raise ValueError('Unsupported predicate operation')
