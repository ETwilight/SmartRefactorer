"""One run namespace; method formats and checkpoint semantics remain method-owned."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import uuid

RESULTS_ROOT = Path(__file__).resolve().parents[1] / 'results'


@dataclass(frozen=True)
class RunLayout:
    root: Path

    @property
    def state(self):
        return self.root / 'method' / 'state'

    @property
    def checkpoints(self):
        return self.root / 'method' / 'checkpoints'

    @property
    def artifacts(self):
        return self.root / 'method' / 'artifacts'

    @property
    def environment(self):
        return self.root / 'environment'

    def prepare(self):
        for path in (
            self.root,
            self.state,
            self.checkpoints,
            self.artifacts,
            self.environment,
            self.root / 'events',
            self.root / 'reports',
            self.root / 'accounting',
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def manifest(self):
        return {
            'schema_version': 1,
            'state_owner': 'method',
            'method_state': 'method/state',
            'method_checkpoints': 'method/checkpoints',
            'method_artifacts': 'method/artifacts',
            'environment': 'environment',
            'resume_policy': 'explicit checkpoint only; never infer a previous run',
        }

    def finish(self, report):
        """Expose common evidence without converting algorithm-owned databases."""
        self.prepare()
        (self.root / 'layout.json').write_text(
            json.dumps(self.manifest(), indent=2) + '\n', encoding='utf-8'
        )
        (self.root / 'accounting' / 'summary.json').write_text(
            json.dumps(report.get('token_usage', {}), indent=2) + '\n', encoding='utf-8'
        )
        (self.checkpoints / 'latest.json').write_text(
            json.dumps(
                {
                    'kind': 'native-state-reference',
                    'state_dir': '../state',
                    'atomic_snapshot': False,
                    'owner': report.get('workflow'),
                    'restore_scope': 'defined by the owning method; no call-stack continuation implied',
                },
                indent=2,
            )
            + '\n',
            encoding='utf-8',
        )
        for source, target in (
            ('report.json', 'reports/report.json'),
            ('report.html', 'reports/report.html'),
            ('log/events.jsonl', 'events/events.jsonl'),
        ):
            path = self.root / source
            if path.is_file():
                if source.endswith('.html'):
                    with path.open('r', encoding='utf-8') as original, (
                        self.root / target
                    ).open('w', encoding='utf-8') as projection:
                        projection.write('<base href="../">')
                        shutil.copyfileobj(original, projection)
                else:
                    shutil.copyfile(path, self.root / target)


def run_identity(output_root=None):
    explicit = os.environ.get('EXPERIMENT_RUN_ID')
    if explicit and not all(c.isalnum() or c in '-_' for c in explicit):
        raise ValueError('Invalid EXPERIMENT_RUN_ID')
    identifier = explicit or str(uuid.uuid4())
    base = Path(os.environ.get('EXPERIMENT_OUTPUT_ROOT') or output_root or RESULTS_ROOT)
    folder = base / 'runs' / identifier
    if explicit and (folder / 'report.json').exists():
        raise FileExistsError(
            'Run already has a report; use an explicit continuation in a new run'
        )
    RunLayout(folder).prepare()
    return identifier, folder
