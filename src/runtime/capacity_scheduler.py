"""Queue runs, claim available environment slots, and collect worker exit results."""

import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from .method_allocation import CAPACITY_EXIT
from audit.recorder import save_json


def write(path, data):
    """Save a JSON file through the shared recorder writer."""

    save_json(path, data)


class SlotLease:
    """Hold an operating-system file lock for one environment slot group.

    path is the shared lock-file Path. stream is None before acquisition and
    after release, or an open binary file while the claim is held. acquire()
    returns immediately with a bool. Closing the stream releases the lock;
    the operating system also releases it when the owning process exits.

    Example:
        lease = SlotLease(claim_path)
        if lease.acquire():
            lease.release()

    A successful acquisition claims this slot group until release(). The
    scheduler holds that claim for the lifetime of the allocated worker.
    """

    def __init__(self, path):
        """Set the lock-file path with no open claim."""

        self.path = Path(path)
        self.stream = None

    def acquire(self):
        """Try to lock the file and return whether this dispatcher acquired it."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # stream changes from None to a binary file holding the operating-system lock.
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
        """Close the claim file and release its operating-system lock."""

        if self.stream:
            self.stream.close()
            self.stream = None


class Scheduler:
    """Dispatch queued jobs as environment capacity becomes available.

    jobs is a list of mutable job dictionaries, including each run directory,
    interpreter, configuration, and queue status. provider supplies the default
    capacity probe. running maps job IDs to (Popen, SlotLease) pairs. A slot
    group admits one job at a time; max_parallel also limits this dispatcher.
    save() persists queue state, tick() processes exits and launches jobs, and
    run() repeats until all jobs leave the queued or running states.

    Example:
        scheduler = Scheduler(folder, provider, jobs)
        finished = scheduler.run(provider.slots(ports))

    finished contains the updated job records, including worker exit codes and
    available report summaries. Task acceptance is recorded in those reports.
    """

    def __init__(
        self, folder, provider, jobs, *, max_parallel=4, poll_seconds=5, probe=None, launch=None
    ):
        """Bind the queue, provider, launch callbacks, and polling limits."""

        if max_parallel < 1 or poll_seconds <= 0:
            raise ValueError('Invalid scheduler limits')
        self.folder = Path(folder)
        self.provider = provider
        self.jobs = jobs
        self.max_parallel = max_parallel
        self.poll_seconds = poll_seconds
        self.probe = probe or provider.probe
        self.launch = launch or self._launch
        # running: dict[str, tuple[Popen, SlotLease]], keyed by job ID.
        self.running = {}
        self.last_wait = None

    def save(self):
        """Persist the current jobs and number of active workers to queue.json."""

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
        """Write the slot-specific configuration and start the allocation worker."""

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
        """Collect worker exits, launch queued jobs into free slots, and report pending work."""

        for identifier, entry in list(self.running.items()):
            process, lease = entry
            # code: int | None; None means the worker is still running.
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
        # active_groups: set[str] of environment slot groups currently assigned to workers.
        active_groups = {j['group'] for j in self.jobs if j['status'] == 'running'}
        for job in self.jobs:
            if job['status'] != 'queued' or len(self.running) >= self.max_parallel:
                continue
            reasons = []
            for slot in slots:
                if slot['group'] in active_groups:
                    continue
                # Reserve each running controller and local JVM allocation during startup,
                # including the interval before their full memory use appears in process
                # measurements.

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
        """Poll the queue to completion; on interruption, cancel queued jobs and request
        worker stops.
        """

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
            # Running workers receive control.json and perform their own cleanup.
            raise
        return self.jobs
