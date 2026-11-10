"""Persistent capacity queue. A waiting job has no method process and no token budget use."""

import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from .method_allocation import CAPACITY_EXIT
from audit.recorder import save_json


def write(path, data):
    save_json(path, data)


class SlotLease:
    """OS-released claim shared by concurrent dispatchers; no stale PID lock stealing."""

    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                if self.path.stat().st_size == 0:
                    self.stream.write(b'0')
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def release(self):
        if self.stream:
            self.stream.close()
            self.stream = None


class Scheduler:
    def __init__(
        self, folder, provider, jobs, *, max_parallel=4, poll_seconds=5, probe=None, launch=None
    ):
        if max_parallel < 1 or poll_seconds <= 0:
            raise ValueError('Invalid scheduler limits')
        self.folder = Path(folder)
        self.provider = provider
        self.jobs = jobs
        self.max_parallel = max_parallel
        self.poll_seconds = poll_seconds
        self.probe = probe or provider.probe
        self.launch = launch or self._launch
        self.running = {}
        self.last_wait = None

    def save(self):
        write(
            self.folder / 'queue.json',
            {
                'jobs': self.jobs,
                'active': len(self.running),
                'updated_ns': time.time_ns(),
                'wait_policy': 'wait for either local or remote capacity',
            },
        )

    def _launch(self, job, slot):
        config = {
            **job['configuration'],
            'port': slot['port'],
            'environment_options': {
                **job['configuration'].get('environment_options', {}),
                'server_port': slot['port'],
            },
        }
        config_path = Path(job['run_dir']) / 'launch.json'
        write(config_path, config)
        log = (Path(job['run_dir']) / 'launcher.log').open('ab')
        try:
            process = subprocess.Popen(
                [
                    job['python'],
                    '-X',
                    'utf8',
                    '-m',
                    'runtime.method_allocation',
                    '--config',
                    str(config_path.resolve()),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        finally:
            log.close()
        return process

    def tick(self, slots):
        for identifier, entry in list(self.running.items()):
            process, lease = entry
            code = process.poll()
            if code is None:
                continue
            job = next(j for j in self.jobs if j['id'] == identifier)
            lease.release()
            del self.running[identifier]
            if code == CAPACITY_EXIT:
                job.update(
                    status='queued',
                    last_wait='Capacity changed before allocation; no method started',
                )
            else:
                job.update(
                    status='completed' if code == 0 else 'failed',
                    exit_code=code,
                    finished_ns=time.time_ns(),
                )
                report = Path(job['run_dir']) / 'report.json'
                if report.is_file():
                    result = json.loads(report.read_text(encoding='utf-8'))
                    job['result'] = {
                        k: result.get(k)
                        for k in (
                            'status',
                            'task_successes',
                            'distinct_tasks_passed',
                            'token_usage',
                        )
                    }
            print(
                json.dumps(
                    {
                        'event': 'job.finished',
                        'method': job['method'],
                        'status': job['status'],
                        'exit_code': code,
                    }
                ),
                flush=True,
            )
        active_groups = {j['group'] for j in self.jobs if j['status'] == 'running'}
        for job in self.jobs:
            if job['status'] != 'queued' or len(self.running) >= self.max_parallel:
                continue
            reasons = []
            for slot in slots:
                if slot['group'] in active_groups:
                    continue
                # Include controllers/JVMs whose worker has launched but whose
                # full RSS may not yet be visible. Conservative double counting
                # is preferable to overbooking a local encoder during startup.
                reserved_mb = sum(
                    j['configuration'].get('controller_mb', 512)
                    + (0 if j.get('remote') else j['configuration'].get('heap_mb', 2048) + 640)
                    for j in self.jobs
                    if j['status'] == 'running'
                )
                state = self.probe(
                    slot,
                    job['configuration'].get('heap_mb', 2048),
                    job['configuration'].get('controller_mb', 512),
                    reserved_mb,
                )
                if not state.get('available'):
                    reasons.append(
                        {'port': slot['port'], 'reason': state.get('reason', 'Unavailable')}
                    )
                    continue
                import hashlib

                key = hashlib.sha256(slot['group'].encode()).hexdigest()
                lease = SlotLease(self.folder.parent / '.slot-claims' / (key + '.lock'))
                if not lease.acquire():
                    continue
                try:
                    process = self.launch(job, slot)
                except BaseException:
                    lease.release()
                    raise
                self.running[job['id']] = (process, lease)
                job.update(
                    status='running',
                    port=slot['port'],
                    group=slot['group'],
                    remote=slot.get('remote', False),
                    pid=process.pid,
                    started_ns=time.time_ns(),
                )
                active_groups.add(slot['group'])
                print(
                    json.dumps(
                        {
                            'event': 'job.started',
                            'method': job['method'],
                            'port': slot['port'],
                            'run_dir': job['run_dir'],
                        }
                    ),
                    flush=True,
                )
                break
            else:
                job['last_wait'] = reasons or 'All available slots are claimed'
        self.save()
        return any(j['status'] in ('queued', 'running') for j in self.jobs)

    def run(self, slots):
        self.save()
        try:
            while self.tick(slots):
                if not self.running:
                    print(
                        json.dumps(
                            {
                                'event': 'queue.waiting',
                                'reason': 'No local or remote capacity',
                                'queued': sum(j['status'] == 'queued' for j in self.jobs),
                            }
                        ),
                        flush=True,
                    )
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            for job in self.jobs:
                if job['status'] == 'queued':
                    job['status'] = 'cancelled'
                elif job['status'] == 'running':
                    write(
                        Path(job['run_dir']) / 'control.json',
                        {'stop': True, 'reason': 'Scheduler cancelled'},
                    )
            self.save()
            # Running hosts retain ownership and cooperative cleanup; no foreign process is killed.
            raise
        return self.jobs
