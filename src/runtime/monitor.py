"""Persistent, read-only experiment observation plus explicit cooperative stops.

No provider calls, game requests, process killing, budget changes or restarts.
Only definitive infrastructure failures authorize a stop; lack of learning
progress is reported for operator review instead.
"""

import argparse
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from audit.recorder import save_json, budget_bounded

TERMINAL = {'completed', 'failed', 'cancelled'}
SYSTEM_STATUSES = {
    'source_revision_changed',
    'runtime_revision_changed',
    'accounting_blocked',
    'task_workspace_transition_uncertain',
    'task_workspace_release_failed',
    'pending_request_unresolved',
    'round_finalization_failed',
    'allocation_failed',
}
SYSTEM_ERRORS = {
    'EnvironmentRevisionChanged',
    'AdapterRevisionChanged',
    'SourceRevisionChanged',
    'TaskWorldError',
    'RequestUncertain',
}
SHARED_FAILURES = {
    'source_revision_changed',
    'runtime_revision_changed',
    'accounting_blocked',
    'EnvironmentRevisionChanged',
    'AdapterRevisionChanged',
    'SourceRevisionChanged',
}


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


@contextmanager
def monitor_lease(path):
    stream = path.open('a+b')
    try:
        try:
            if os.name == 'nt':
                import msvcrt

                if path.stat().st_size == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another monitor owns this batch') from exc
        yield
    finally:
        stream.close()


def empty_events():
    return {
        'models': {},
        'round': None,
        'finished_rounds': [],
        'evaluations': {},
        'evaluation_requests': [],
        'fallbacks': {},
        'errors': {},
        'mutation_count': 0,
        'last_execution': None,
        'last_environment_event': None,
        'last_activity': None,
        'system_failures': [],
    }


class BatchMonitor:
    def __init__(self, batch_dir, interval_seconds=600, *, clock=time.time):
        if interval_seconds <= 0:
            raise ValueError('Monitor interval must be positive')
        self.batch_dir = Path(batch_dir).resolve()
        self.interval = interval_seconds
        self.clock = clock
        self.directory = self.batch_dir / 'monitor'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / 'state.json'
        self.errors = []
        self.state = {'runs': {}}
        self.queue = None
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                self.error('monitor.restore', self.state_path, exc)

    def error(self, stage, path, exc):
        item = {
            'time_utc': utc(self.clock()),
            'stage': stage,
            'path': str(path),
            'type': type(exc).__name__,
            'message': str(exc)[:1000],
        }
        self.errors.append(item)
        try:
            with (self.directory / 'errors.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + '\n')
                stream.flush()
        except OSError:
            print(json.dumps({'monitor_error': item}), file=sys.stderr, flush=True)
            raise

    def read_json(self, path, stage):
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self.error(stage, path, exc)
            return None

    @staticmethod
    def _failure(events, reason, evidence):
        item = {'reason': reason, 'evidence': evidence}
        if item not in events['system_failures']:
            events['system_failures'].append(item)

    @staticmethod
    def _mutation(request):
        if request.get('op') != 'action.execute':
            return False
        action = request.get('params', {}).get('action', {})
        method = action.get('method', '')
        return (
            action.get('side') != 'local'
            and not method.startswith('observe.')
            and method
            not in (
                'clock.query',
                'counter.read',
                'command.describe',
                'fake_player.status',
                'fake_player.list',
                'fake_player.inventory',
                'session.describe',
            )
        )

    def event(self, events, value, path):
        kind = value.get('kind')
        when = value.get('time_utc')
        sequence = value.get('sequence')
        if when:
            events['last_activity'] = when
        top_error = value.get('error')
        if isinstance(top_error, dict) and top_error.get('type'):
            name = top_error['type']
            events['errors'][name] = events['errors'].get(name, 0) + 1
            if name in SYSTEM_ERRORS:
                self._failure(events, name, {'file': str(path), 'sequence': sequence})
        call = value.get('call') or {}
        if kind == 'continuation.start':
            fields = (
                'carried_tokens',
                'carried_model_calls',
                'completed_rounds',
                'resumed_round',
            )
            if any(type(value.get(key)) is not int or value[key] < 0 for key in fields):
                self._failure(
                    events,
                    'accounting_blocked',
                    {
                        'file': str(path),
                        'sequence': sequence,
                        'detail': 'Invalid continuation accounting offset',
                    },
                )
            else:
                carried = {key: value[key] for key in fields}
                carried['evaluation_counts'] = value.get('carried_evaluation_counts', {})
                carried['evaluation_requests'] = value.get('carried_evaluation_requests', 0)
                carried['evaluations'] = value.get('carried_evaluations', 0)
                if events.get('continuation') and events['continuation'] != carried:
                    self._failure(
                        events,
                        'accounting_blocked',
                        {
                            'file': str(path),
                            'sequence': sequence,
                            'detail': 'Conflicting continuation accounting offsets',
                        },
                    )
                else:
                    events['continuation'] = carried
                    events['round'] = carried['resumed_round']
        if kind in ('model.start', 'model.finish', 'model.not_sent'):
            identifier = call.get('call_id')
            if identifier:
                events['models'][identifier] = {
                    'scope': call.get('scope'),
                    'state': kind,
                    'tokens': call.get('charged_tokens', 0) if kind != 'model.start' else 0,
                    'reserved_tokens': (
                        call.get('reserved_tokens', 0) if kind == 'model.start' else 0
                    ),
                    'usage_status': call.get('usage_status'),
                    'budget_bounded': budget_bounded(call),
                    'time_utc': when,
                }
            if kind == 'model.finish' and (
                call.get('accounting_error')
                or (
                    call.get('usage_status') == 'unknown_reserved_upper_bound'
                    and not budget_bounded(call)
                )
            ):
                self._failure(
                    events,
                    'accounting_blocked',
                    {
                        'file': str(path),
                        'sequence': sequence,
                        'call_id': identifier,
                        'usage_status': call.get('usage_status'),
                    },
                )
            error = call.get('error') or {}
            if isinstance(error, dict) and error.get('type'):
                name = error['type']
                events['errors'][name] = events['errors'].get(name, 0) + 1
                if name in SYSTEM_ERRORS:
                    self._failure(
                        events,
                        name,
                        {'file': str(path), 'sequence': sequence, 'call_id': identifier},
                    )
        if kind == 'round.start':
            events['round'] = value.get('round')
        if kind == 'round.finish':
            number = value.get('round') or (value.get('record') or {}).get('round')
            if number not in events['finished_rounds']:
                events['finished_rounds'].append(number)
        if kind in ('env.start', 'env.finish'):
            request = call.get('request') or {}
            op = request.get('op')
            rid = request.get('request_id')
            action = request.get('params', {}).get('action', {})
            events['last_environment_event'] = {
                'time_utc': when,
                'op': op,
                'method': action.get('method'),
                'request_id': rid,
                'stage': call.get('stage'),
                'kind': kind,
            }
            if kind == 'env.start':
                if op == 'evaluation.run' and rid not in events['evaluation_requests']:
                    events['evaluation_requests'].append(rid)
                if self._mutation(request):
                    events['mutation_count'] += 1
                    events['last_execution'] = {
                        'time_utc': when,
                        'request_id': rid,
                        'method': action.get('method'),
                    }
            feedback = call.get('feedback') or {}
            report = feedback.get('result')
            if (
                kind == 'env.finish'
                and op in ('evaluation.run', 'evaluation.get')
                and isinstance(report, dict)
                and report.get('evaluation_id')
            ):
                events['evaluations'][report['evaluation_id']] = {
                    'verdict': report.get('verdict'),
                    'time_utc': when,
                }
            error = feedback.get('error') or call.get('error') or {}
            if isinstance(error, dict) and error.get('type'):
                name = error['type']
                events['errors'][name] = events['errors'].get(name, 0) + 1
                if name in SYSTEM_ERRORS:
                    self._failure(
                        events,
                        name,
                        {'file': str(path), 'sequence': sequence, 'request_id': rid},
                    )
        if kind == 'method.evidence_loop_fallback':
            purpose = value.get('purpose', value.get('scope', 'unknown'))
            events['fallbacks'][purpose] = events['fallbacks'].get(purpose, 0) + 1
        if kind == 'budget.blocked' and value.get('limit') == 'accounting':
            self._failure(
                events, 'accounting_blocked', {'file': str(path), 'sequence': sequence}
            )
        if kind in ('operator.stop', 'run.finish'):
            reason = value.get('reason', value.get('status'))
            if reason in SYSTEM_STATUSES:
                self._failure(events, reason, {'file': str(path), 'sequence': sequence})

    def consume_events(self, run_dir, state):
        path = run_dir / 'log/events.jsonl'
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        identity = [stat.st_dev, stat.st_ino]
        cursor = state.setdefault('cursor', {'offset': 0, 'identity': identity})
        if cursor.get('identity') != identity or stat.st_size < cursor['offset']:
            state['events'] = empty_events()
            cursor = {'offset': 0, 'identity': identity}
            state['cursor'] = cursor
        events = state.setdefault('events', empty_events())
        with path.open('rb') as stream:
            stream.seek(cursor['offset'])
            while True:
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    break
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('Event record must be an object')
                except (ValueError, UnicodeError) as exc:
                    self.error('events.parse', path, exc)
                else:
                    self.event(events, value, path)
                cursor['offset'] = stream.tell()

    def observe_run(self, job, now):
        run_dir = Path(job['run_dir']).resolve()
        # Only the named batch's standard run tree can receive stop controls.
        if not run_dir.is_relative_to(self.batch_dir.parent.parent / 'runs'):
            raise ValueError('Batch run directory is outside its results/runs tree')
        identifier = job['id']
        state = self.state['runs'].setdefault(identifier, {'events': empty_events()})
        self.consume_events(run_dir, state)
        events = state['events']
        report = self.read_json(run_dir / 'report.json', 'report.read')
        if report is not None:
            state['report'] = {
                k: report.get(k)
                for k in ('status', 'finished_at', 'error', 'model_calls', 'evaluation_counts')
            }
            state['report']['rounds'] = len(report.get('rounds', []))
            state['report']['token_usage'] = report.get('token_usage', {})
        report = state.get('report', {})
        accounting = (
            self.read_json(run_dir / 'accounting/summary.json', 'accounting.read') or {}
        )
        worker = self.read_json(run_dir / 'worker-error.json', 'worker_error.read') or {}
        allocation = self.read_json(run_dir / 'allocation.json', 'allocation.read') or {}
        failures = list(events['system_failures'])
        for name, document in [
            ('report.json', report),
            ('allocation.json', allocation),
            ('worker-error.json', worker),
        ]:
            status = document.get('status')
            error = document.get('error') or document
            if status in SYSTEM_STATUSES:
                failures.append({'reason': status, 'evidence': {'file': str(run_dir / name)}})
            if isinstance(error, dict) and error.get('type') in SYSTEM_ERRORS:
                failures.append(
                    {'reason': error['type'], 'evidence': {'file': str(run_dir / name)}}
                )
        artifacts = run_dir / 'method/artifacts'
        executions = []
        if artifacts.is_dir():
            # Stat only: input/events files contain large private model traces.
            for path in artifacts.glob('execution-*/input.json'):
                finished = path.with_name('events.json')
                latest_time = max(
                    path.stat().st_mtime, finished.stat().st_mtime if finished.exists() else 0
                )
                executions.append((latest_time, str(path.parent.relative_to(run_dir))))
        latest = max(executions, default=None)
        models = events['models']
        usage = accounting or report.get('token_usage') or {}
        carried = events.get('continuation', {})
        charged = (
            carried.get('carried_tokens', 0) + sum(item['tokens'] for item in models.values())
            if models or carried
            else usage.get('charged_tokens', 0)
        )
        calls = (
            carried.get('carried_model_calls', 0) + len(models)
            if models or carried
            else report.get('model_calls')
            or sum(v.get('calls', 0) for v in usage.get('by_scope', {}).values())
        )
        metric_source = (
            'continuation_and_segment_events'
            if carried
            else 'events' if models else 'accounting_or_report'
        )
        if report.get('finished_at') and isinstance(report.get('token_usage'), dict):
            # A final authoritative report can survive a lost/truncated event.
            charged = report['token_usage'].get('charged_tokens', charged)
            calls = report.get('model_calls') or calls
            metric_source = 'final_report'
        marker = [
            len(executions) if executions else events['mutation_count'],
            len(events['evaluations']),
        ]
        baseline = state.get('review')
        if baseline is None or marker != baseline['progress']:
            baseline = {
                'progress': marker,
                'time': now,
                'idle_intervals': 0,
                'fallback_base': dict(events['fallbacks']),
            }
        elif now - baseline['time'] >= self.interval:
            baseline['idle_intervals'] += 1
            baseline['time'] = now
        state['review'] = baseline
        repeated = [
            purpose
            for purpose, count in events['fallbacks'].items()
            if count - baseline['fallback_base'].get(purpose, 0) >= 2
        ]
        review = baseline['idle_intervals'] >= 2 and bool(repeated)
        last_activity = events['last_activity']
        if report.get('finished_at'):
            last_activity = max(
                filter(None, [last_activity, report['finished_at']]), default=None
            )
        if latest:
            last_activity = max(filter(None, [last_activity, utc(latest[0])]), default=None)
        errors = Counter(events['errors'])
        for error in [report.get('error'), worker.get('error', worker)]:
            if isinstance(error, dict) and error.get('type'):
                errors[error['type']] += 1
        uncertainty = {
            'unknown_usage_upper_bound_tokens': sum(
                item['tokens']
                for item in models.values()
                if item.get('usage_status') == 'unknown_reserved_upper_bound'
            ),
            'bounded_unknown_calls': sum(
                item.get('usage_status') == 'unknown_reserved_upper_bound'
                and item.get('budget_bounded', False)
                for item in models.values()
            ),
            'unbounded_unknown_calls': sum(
                item.get('usage_status') == 'unknown_reserved_upper_bound'
                and not item.get('budget_bounded', False)
                for item in models.values()
            ),
            'usage_exact_complete': (
                all(
                    item.get('usage_status') in ('reported', 'not_sent')
                    for item in models.values()
                )
                if models
                else usage.get('complete')
            ),
            'budget_bounded_complete': (
                all(
                    item.get(
                        'budget_bounded', item.get('usage_status') in ('reported', 'not_sent')
                    )
                    for item in models.values()
                )
                if models
                else usage.get('budget_bounded_complete')
            ),
        }
        if metric_source == 'final_report':
            full = report['token_usage']
            for field in (
                'unknown_usage_upper_bound_tokens',
                'bounded_unknown_calls',
                'unbounded_unknown_calls',
                'budget_bounded_complete',
            ):
                if field in full:
                    uncertainty[field] = full[field]
            if 'complete' in full:
                uncertainty['usage_exact_complete'] = full['complete']
        return {
            'id': identifier,
            'method': job.get('method'),
            'environment': job.get('configuration', {}).get('environment', 'unknown'),
            'run_dir': str(run_dir),
            'queue_status': job.get('status'),
            'status': report.get('status') or allocation.get('status') or job.get('status'),
            'terminal': job.get('status') in TERMINAL,
            'charged_tokens': charged,
            'model_calls': calls,
            'metrics_source': metric_source,
            'inflight_model_calls': sum(
                item['state'] == 'model.start' for item in models.values()
            ),
            'inflight_reserved_tokens': sum(
                item.get('reserved_tokens', 0)
                for item in models.values()
                if item['state'] == 'model.start'
            ),
            **uncertainty,
            'round': events['round'],
            'completed_rounds': max(
                carried.get('completed_rounds', 0) + len(events['finished_rounds']),
                report.get('rounds', 0),
            ),
            'carried_tokens': carried.get('carried_tokens', 0),
            'segment_charged_tokens': sum(item['tokens'] for item in models.values()),
            'evaluation_requests': carried.get('evaluation_requests', 0)
            + len(events['evaluation_requests']),
            'evaluations': carried.get('evaluations', 0) + len(events['evaluations']),
            'evaluation_counts': dict(
                Counter(carried.get('evaluation_counts', {}))
                + Counter(v['verdict'] for v in events['evaluations'].values())
            ),
            'executions': marker[0],
            'last_execution': (
                {'artifact': latest[1], 'time_utc': utc(latest[0])}
                if latest
                else events['last_execution']
            ),
            'last_environment_event': events['last_environment_event'],
            'fallbacks': dict(events['fallbacks']),
            'error_categories': dict(errors),
            'latest_activity': last_activity,
            'system_failures': failures,
            'needs_operator_review': review,
            'review_purposes': repeated if review else [],
            'no_progress_intervals': baseline['idle_intervals'],
        }

    def stop(self, run, causes, now):
        path = Path(run['run_dir']) / 'control.json'
        existing = {}
        if path.exists():
            existing = self.read_json(path, 'control.read')
            if existing is None:
                raise ValueError('Refusing to replace an unreadable existing run control')
        if existing.get('stop') is True:
            return {
                'run_id': run['id'],
                'action': 'already_stopping',
                'control': str(path),
                'causes': causes,
            }
        if set(existing) - {'max_rounds', 'stop', 'reason'}:
            raise ValueError('Refusing to replace a control with unsupported fields')
        reason = 'Monitor: confirmed system failure affecting this run. Preserve native state and world; no automatic restart or budget renewal.'
        # RunControl accepts only its public control fields. Audit provenance
        # belongs in a sidecar, otherwise the stop becomes operator_control_error.
        receipt = (
            self.directory
            / 'interventions'
            / (run['id'] + '-' + str(int(now * 1000000)) + '.json')
        )
        save_json(
            receipt,
            {
                'time_utc': utc(now),
                'batch_dir': str(self.batch_dir),
                'run_id': run['id'],
                'causes': causes,
            },
        )
        save_json(path, {**existing, 'stop': True, 'reason': reason})
        return {
            'run_id': run['id'],
            'action': 'cooperative_stop_requested',
            'control': str(path),
            'time_utc': utc(now),
            'causes': causes,
            'receipt': str(receipt),
        }

    def snapshot(self):
        self.errors = []
        now = self.clock()
        queue = self.read_json(self.batch_dir / 'queue.json', 'queue.read')
        if queue is not None:
            self.queue = queue
        jobs = (self.queue or {}).get('jobs', [])
        runs = []
        for job in jobs:
            try:
                runs.append(self.observe_run(job, now))
            except Exception as exc:
                self.error('run.observe', job.get('run_dir', 'unknown'), exc)
        actions = []
        for run in runs:
            if run['terminal']:
                continue
            causes = [
                {'run_id': source['id'], **failure}
                for source in runs
                if source['environment'] == run['environment']
                for failure in source['system_failures']
                if source['id'] == run['id']
                or (run['environment'] != 'unknown' and failure['reason'] in SHARED_FAILURES)
            ]
            peer_failures = [
                {'run_id': source['id'], **failure}
                for source in runs
                if source['environment'] == run['environment'] and source['id'] != run['id']
                for failure in source['system_failures']
                if failure['reason'] not in SHARED_FAILURES
            ]
            if peer_failures:
                run['needs_operator_review'] = True
                run['peer_system_failures'] = peer_failures
            if causes:
                try:
                    actions.append(self.stop(run, causes, now))
                except Exception as exc:
                    self.error('run.stop', run['run_dir'], exc)
        final = bool(jobs) and len(runs) == len(jobs) and all(run['terminal'] for run in runs)
        result = {
            'schema_version': 1,
            'time_utc': utc(now),
            'pid': os.getpid(),
            'interval_seconds': self.interval,
            'state': 'complete' if final else 'observing' if jobs else 'waiting_for_queue',
            'final': final,
            'runs': runs,
            'actions': actions,
            'monitor_errors': list(self.errors),
            'policy': 'No budget renewal or restart; algorithm failures and long calls do not trigger stops.',
        }
        save_json(self.state_path, self.state)
        save_json(self.directory / 'status.json', result)
        with (self.directory / 'observations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
            stream.flush()
        return result

    def run(self, once=False):
        with monitor_lease(self.directory / 'monitor.lock'):
            return self._run(once)

    def _run(self, once=False):
        while True:
            try:
                result = self.snapshot()
            except Exception as exc:
                self.error('monitor.snapshot', self.directory, exc)
                if once:
                    return 1
            else:
                if once or result['final']:
                    return int(bool(result['monitor_errors']))
            time.sleep(self.interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-dir', type=Path, required=True)
    parser.add_argument('--interval-seconds', type=float, default=600)
    parser.add_argument(
        '--once', action='store_true', help='Write one offline snapshot without waiting'
    )
    args = parser.parse_args(argv)
    try:
        return BatchMonitor(args.batch_dir, args.interval_seconds).run(args.once)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            json.dumps({'monitor_error': {'type': type(exc).__name__, 'message': str(exc)}}),
            file=sys.stderr,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
