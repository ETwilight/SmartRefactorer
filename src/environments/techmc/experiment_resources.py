"""On-demand, credential-scoped runner memory. No PID/path is accepted from agents."""

import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

if __package__:
    from .experiment_registry import directory
else:
    from experiment_registry import directory


def last_sample(path):
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 65536))
        tail = stream.read(65536)
    lines = tail.split(b'\n')[:-1]  # Ignore an in-progress final write.
    for line in reversed(lines):
        try:
            return json.loads(line)
        except (ValueError, UnicodeError):
            continue
    return None


def read(environment):
    remote = getattr(environment.session.server, 'remote', None)
    identifier = getattr(remote, 'experiment_id', None) or os.environ.get('MC_EXPERIMENT_ID')
    unavailable = {
        'status': 'unavailable',
        'scope': 'This experiment runner process only; excludes child workers and remote model-service memory',
    }
    if not identifier:
        return {**unavailable, 'reason': 'No experiment host binding'}
    try:
        folder = directory(environment.root, identifier)
        credential = Path(environment.session.server.token_file).read_text().strip()
        if not secrets.compare_digest(credential, (folder / 'owner.token').read_text().strip()):
            return {**unavailable, 'reason': 'Experiment ownership mismatch'}
        record = json.loads((folder / 'manager.json').read_text(encoding='utf-8'))
        if (
            record.get('experiment_id') != identifier
            or record.get('port') != urlsplit(environment.session.server.url).port
        ):
            return {**unavailable, 'reason': 'Experiment binding mismatch'}
        sample = last_sample(folder / 'local-memory.jsonl')
        if not sample or not sample.get('runner'):
            return {**unavailable, 'reason': 'No registered runner memory sample'}
        age = max(0, (time.time_ns() - sample['wall_ns']) / 1_000_000_000)
        runner = sample['runner']
        limit = record.get('runner_memory_limit_bytes')
        limit = limit if type(limit) is int and limit > 0 else None
        rss = runner.get('rss_bytes')
        pressure = (
            'unknown'
            if limit is None or rss is None or age > 15
            else 'high' if rss >= limit * 0.9 else 'normal'
        )
        return {
            'status': 'stale' if age > 15 else 'sampled',
            'scope': unavailable['scope'],
            'rss_bytes': rss,
            'peak_rss_bytes': runner.get('os_peak_rss_bytes'),
            'declared_limit_bytes': limit,
            'pressure': pressure,
            'sample_age_seconds': age,
            'limit_semantics': 'Host-declared process limit when supplied; null means unknown, not unlimited',
            'hint': (
                'Reduce retained evidence or worker memory; notify the host if pressure persists'
                if pressure == 'high'
                else None
            ),
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {**unavailable, 'reason': 'Resource sample unavailable: ' + type(exc).__name__}
