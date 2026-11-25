"""Record process/host memory samples, reported JVM peaks, and exit diagnostics."""

import json
import os
from pathlib import Path
import subprocess
import threading
import time

if __package__:
    from .host_resources import memory, process_memory
    from .experiment_registry import write
else:
    from host_resources import memory, process_memory
    from experiment_registry import write


class Monitor:
    """Sample one process and retain memory summaries alongside the host records.

    pid is the sampled process ID and directory is its existing output Path.
    interval sets the sampling period. summary restores memory-summary.json or
    starts with empty counters; it retains sampled RSS peaks, OS high-water
    marks, host-availability minima, and heap/nonheap peaks supplied by heap().
    lock serializes updates and stop_event ends the background loop.

    start() assigns and starts thread. sample() writes JSONL, CSV, and the
    current summary; heap() updates its in-memory JVM peaks. close() stops and
    joins the thread, adds the platform child-process peak when available,
    and writes the final summary.

    Example:
        monitor = Monitor(pid, directory).start()
        monitor.close()

    The monitor starts periodic OS sampling, then stops and saves its accumulated
    summary. The owning experiment keeps it running across the server lifecycle.
    """

    def __init__(self, pid, directory, interval=1):
        """Bind the process, output directory, sampling interval, and restored peak summary."""

        self.pid = pid
        self.directory = Path(directory)
        self.interval = interval
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        path = self.directory / 'memory-summary.json'
        self.summary = (
            json.loads(path.read_text())
            if path.exists()
            else {'samples': 0, 'sampled_peak_rss_bytes': 0, 'os_peak_rss_bytes': 0}
        )
        self.summary.update(
            sample_interval_seconds=interval,
            peak_scope='OS process high-water marks plus sampled heap; not an exact instantaneous heap maximum',
        )

    def sample(self):
        """Take and persist one process/host memory sample while holding the monitor lock."""

        with self.lock:
            self._sample()

    def _sample(self):
        """Update peak counters and append the sampled values or read error to JSONL and CSV
        files.
        """
        # sample: dict of timestamp/PID plus memory counters or their read-error message.

        sample = {'wall_ns': time.time_ns(), 'pid': self.pid}
        try:
            sample.update(process_memory(self.pid))
            sample['host'] = memory()
            self.summary['samples'] += 1
            for key in ('rss_bytes', 'os_peak_rss_bytes'):
                target = 'sampled_peak_rss_bytes' if key == 'rss_bytes' else key
                self.summary[target] = max(self.summary.get(target, 0), sample.get(key, 0))
            self.summary['minimum_host_available_bytes'] = min(
                self.summary.get('minimum_host_available_bytes', 2**63),
                sample['host']['available_bytes'],
            )
        except (OSError, ValueError) as exc:
            sample['error'] = str(exc)
        with (self.directory / 'memory.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(sample) + '\n')
        self.summary['last_sample_ns'] = sample['wall_ns']
        write(self.directory / 'memory-summary.json', self.summary)
        import csv

        path = self.directory / 'memory.csv'
        exists = path.exists()
        with path.open('a', newline='') as stream:
            writer = csv.writer(stream)
            if not exists:
                writer.writerow(
                    [
                        'wall_ns',
                        'pid',
                        'rss_bytes',
                        'os_peak_rss_bytes',
                        'host_available_bytes',
                        'error',
                    ]
                )
            writer.writerow(
                [
                    sample['wall_ns'],
                    self.pid,
                    sample.get('rss_bytes'),
                    sample.get('os_peak_rss_bytes'),
                    sample.get('host', {}).get('available_bytes'),
                    sample.get('error', ''),
                ]
            )

    def heap(self, value):
        """Update sampled JVM heap and nonheap peaks from the supplied telemetry."""

        with self.lock:
            self.summary['sampled_peak_heap_bytes'] = max(
                self.summary.get('sampled_peak_heap_bytes', 0), value['heap_used_bytes']
            )
            self.summary['sampled_peak_nonheap_bytes'] = max(
                self.summary.get('sampled_peak_nonheap_bytes', 0),
                value.get('nonheap_used_bytes', 0),
            )

    def start(self):
        """Start the background sampling thread and return this monitor."""

        def work():
            """Sample at the configured interval until stopped, retaining sampling errors in
            the summary.
            """

            while not self.stop_event.is_set():
                try:
                    self.sample()
                except Exception as exc:
                    self.summary['monitor_error'] = str(exc)
                self.stop_event.wait(self.interval)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return self

    def close(self):
        """Stop sampling, join its thread, add available child-process peak data, and save
        the final summary.
        """

        self.stop_event.set()
        self.thread.join(timeout=5)
        if os.name != 'nt':
            import resource

            self.summary['child_process_peak_rss_bytes'] = (
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
            )
        write(self.directory / 'memory-summary.json', self.summary)


def diagnostics(directory, exit_code, error=None):
    """Classify the supplied exit from log/code evidence and save process, JVM-artifact, and
    available kernel-log diagnostics.
    """

    directory = Path(directory)
    path = directory / 'supervisor.log'
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 131072))
            tail = stream.read().decode('utf-8', errors='replace')
    else:
        tail = ''
    reason = 'normal_exit' if exit_code == 0 else 'unknown_nonzero_exit'
    if 'OutOfMemoryError' in tail:
        reason = 'java_out_of_memory'
    elif exit_code == -9:
        reason = 'sigkill_unconfirmed_cause'
    elif exit_code is not None and exit_code < 0:
        reason = 'signal_' + str(-exit_code)
    if error and exit_code == 0:
        reason = 'supervisor_error_after_graceful_stop'
    result = {
        'exit_code': exit_code,
        'classification': reason,
        'supervisor_error': error,
        'fatal_error_files': [str(p) for p in directory.glob('hs_err_pid*.log')],
        'heap_dump_files': [str(p) for p in directory.glob('*.hprof')],
        'classification_is_inference': reason not in ('normal_exit', 'java_out_of_memory'),
    }
    if exit_code != 0 and os.name != 'nt':
        try:
            journal = subprocess.run(
                ['journalctl', '-k', '--no-pager', '-n', '200'],
                capture_output=True,
                text=True,
                timeout=8,
            )
            lines = [
                line
                for line in journal.stdout.splitlines()
                if any(
                    word in line.lower() for word in ('out of memory', 'oom', 'killed process')
                )
            ]
            result['kernel_oom_evidence'] = {
                'returncode': journal.returncode,
                'matching_lines': lines,
                'diagnostic': journal.stderr[-2000:],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            result['kernel_oom_evidence'] = {'unavailable': str(exc)}
    write(directory / 'failure-report.json', result)
    return result
