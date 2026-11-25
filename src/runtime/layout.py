"""Create each run directory and publish its shared report, event, and accounting views."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import uuid

from .storage import RESULTS_ROOT


@dataclass(frozen=True)
class RunLayout:
    """Name the method, environment, and report directories within one run.

    root is the run's Path. Method state, checkpoints, and artifacts have separate
    subdirectories; the owning method defines the contents and restore behavior.
    prepare() creates the directories. finish() writes the shared layout and
    accounting files, then copies the available report and event views.

    Example:
        layout = RunLayout(run_folder).prepare()
        state_folder = layout.state

    state_folder is where the method stores its own persistent state files.
    """

    root: Path

    @property
    def state(self):
        """Return the directory for persistent method state."""

        return self.root / 'method' / 'state'

    @property
    def checkpoints(self):
        """Return the directory for method checkpoints and checkpoint references."""

        return self.root / 'method' / 'checkpoints'

    @property
    def artifacts(self):
        """Return the directory for method-generated artifacts."""

        return self.root / 'method' / 'artifacts'

    @property
    def environment(self):
        """Return the directory for this run's environment records."""

        return self.root / 'environment'

    def prepare(self):
        """Create the run directories and return this layout."""

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
        """Describe the shared relative paths and explicit checkpoint selection policy."""

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
        """Write accounting and layout metadata, then copy available report and event files."""
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
    """Choose a run ID, create its directory, and return the ID and directory Path."""

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
