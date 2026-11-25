"""Configure category-filtered session observations and read their saved event sequence."""

import json
from pathlib import Path
import re
import time

if __package__ and __package__.startswith('environments.techmc.'):
    from ..runtime_support import ROOT, atomic_json
else:
    from runtime_support import ROOT, atomic_json


class Trace:
    """Control the optional observation stream for an existing session run ID.

    directory is the session Path under TechMC data runs. start() writes category
    filters in trace-config.json; ActionSession.event() reads that configuration
    and copies selected future events into observations.jsonl. stop() disables
    copying. read() reads saved observations after a sequence cursor. The
    session's full invocation trace is managed separately by ActionSession.

    Example:
        trace = Trace(run_id)
        trace.start(categories=['invocation.end'])
        events = trace.read(after_sequence=0, limit=5)
        trace.stop()

    start() enables copying completed-invocation events. events contains up to
    five observations already present in the saved stream when read() runs.
    """

    def __init__(self, run_id):
        """Validate the run ID and select its existing session directory."""

        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id):
            raise ValueError('Invalid run ID')
        self.directory = ROOT / 'runs' / run_id

    def start(self, categories=None, region=None):
        """Enable observation recording for the selected event categories."""

        if region is not None:
            raise ValueError(
                'Regional internal event instrumentation is not enabled; action trace supports category filtering only'
            )
        config = {
            'categories': categories or ['invocation.start', 'invocation.end'],
            'region': region,
            'started_ns': time.time_ns(),
            'active': True,
            'scope': 'adapter invocations; mandatory audit trace.jsonl is always retained',
        }
        atomic_json(self.directory / 'trace-config.json', config)
        return config

    def stop(self):
        """Disable observation recording and save its stop timestamp."""

        config = json.loads((self.directory / 'trace-config.json').read_text())
        config['active'] = False
        config['stopped_ns'] = time.time_ns()
        atomic_json(self.directory / 'trace-config.json', config)
        return config

    def read(self, after_sequence=0, limit=100):
        """Read observations after the sequence cursor, with the requested limit capped at
        1000.
        """

        path = self.directory / 'observations.jsonl'
        if not path.exists():
            return []
        result = []
        with path.open(encoding='utf-8') as f:
            for line in f:
                event = json.loads(line)
                if event['sequence'] > after_sequence:
                    result.append(event)
                    if len(result) >= min(limit, 1000):
                        break
        return result
