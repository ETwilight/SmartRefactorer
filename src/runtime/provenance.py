"""Record environment source and asset fingerprints, archive their initial bytes, and track
edits.
"""

import hashlib
from pathlib import Path
from runtime.context.codec import compact_json
from audit.recorder import save_json, utc_now


class EnvironmentRevisionChanged(RuntimeError):
    """Report a monitored environment file change detected by check().

    The exception message identifies the revision record used by the runner.
    check() raises this after observe() has recorded the changed file entries.

    Example:
        revision.check()

    If a monitored file changed after revision was created, this call raises
    EnvironmentRevisionChanged and the runner stops the current run.
    """

    pass


class EnvironmentRevision:
    """Track the initial and subsequent contents of an environment's selected files.

    root is the resolved environment asset Path. PATTERNS selects source,
    interface, build, task, and installed-asset records. For TechMC, capture()
    also includes configured data files and the separate Python source package.
    start and latest are fingerprint manifests; changes maps file names to
    their before/after records and retains previously detected changes.

    original_bytes holds dict[str, bytes] until attach() writes the initial
    source archive. path and recorder are None until attach() binds them.
    check() raises EnvironmentRevisionChanged after detecting a change;
    finalize() writes a last observation and returns the report summary.

    Example:
        revision = EnvironmentRevision(ASSET_ROOT)
        revision.attach(run_folder / 'environment-revision.json', recorder)
        revision.check()

    The run now has its initial source archive and a manifest that check()
    compares against current file contents before further work.
    """

    PATTERNS = (
        '*.py',
        'adapters/*.py',
        'acceptance/*.py',
        'actions/*.py',
        'tasks/*.json',
        'schemas/*.json',
        'knowledge/procedures/*/SKILL.md',
        'INTERFACE_LANGUAGE.md',
        'KNOWLEDGE_ACCESS.md',
        'bridge/build/minecraft-agent-26.1.jar',
        'runtime/void-research-server.json',
        'runtime/mods.candidate-lock.json',
        'runtime/java.json',
        'knowledge/wiki/zim/*.provenance.json',
        'knowledge/minecraft/26.1/*.json',
    )

    def __init__(self, root):
        """Capture the initial manifest and bytes for the selected environment files."""

        self.root = Path(root).resolve()
        self.started_at = utc_now()
        if not self.root.is_dir():
            raise ValueError('Environment source root does not exist')
        self.start, self.original_bytes = self.capture(keep_bytes=True)
        self.latest = self.start
        self.changes = {}
        self.path = None
        self.recorder = None

    def capture(self, keep_bytes=False):
        """Return a fingerprint manifest and, when requested, the selected file bytes."""

        paths = {p for pattern in self.PATTERNS for p in self.root.glob(pattern) if p.is_file()}
        files = {}
        # contents: dict[str, bytes]; populated only when keep_bytes is true.
        contents = {}
        # named: dict[str, Path], mapping archive-relative names to their current file paths.
        named = {path.relative_to(self.root).as_posix(): path for path in paths}
        # TechMC source, source assets, and installed data have separate roots.
        # Archive Python source under code/ and use data-relative names for installed assets.
        source = self.root.parent.parent / 'environments/techmc'
        if self.root.name == 'techmc' and self.root.parent.name == 'assets' and source.is_dir():
            from environments.techmc.paths import ASSET_ROOT, DATA_ROOT

            named.update(
                {
                    p.relative_to(DATA_ROOT).as_posix(): p
                    for pattern in self.PATTERNS
                    for p in DATA_ROOT.glob(pattern)
                    if self.root == ASSET_ROOT.resolve()
                    if p.is_file()
                }
            )
            named.update(
                {
                    'code/' + p.relative_to(source).as_posix(): p
                    for pattern in ('*.py', 'adapters/*.py', 'acceptance/*.py', 'actions/*.py')
                    for p in source.glob(pattern)
                    if p.is_file()
                }
            )
        for name, path in sorted(named.items()):
            try:
                data = path.read_bytes()
                files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                if keep_bytes:
                    contents[name] = data
            except OSError as exc:
                files[name] = {'unreadable': type(exc).__name__}
        return {
            'fingerprint': hashlib.sha256(compact_json(files).encode()).hexdigest(),
            'files': files,
        }, contents

    def attach(self, path, recorder):
        """Archive the initial bytes, bind the revision record and recorder, then check for
        edits.
        """

        self.path = Path(path)
        self.recorder = recorder
        archive = self.path.parent / 'environment-source'
        for name, data in self.original_bytes.items():
            target = archive / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # The initial file bytes are now in environment-source/; release the in-memory copies.
        self.original_bytes = {}
        self.save()
        self.check()

    def observe(self):
        """Compare current files with the initial manifest and record newly observed changes."""

        self.latest, _ = self.capture()
        old = self.start['files']
        new = self.latest['files']
        found = []
        for name in sorted(old.keys() | new.keys()):
            if old.get(name) != new.get(name):
                change = {'file': name, 'before': old.get(name), 'after': new.get(name)}
                if self.changes.get(name) != change:
                    found.append(change)
                self.changes[name] = change
        unreadable = [name for name, value in new.items() if 'unreadable' in value]
        for name in unreadable:
            self.changes[name] = {'file': name, 'before': old.get(name), 'after': new[name]}
        if found or unreadable:
            if self.recorder is not None:
                self.recorder.event(
                    'environment.revision_changed', changes=found, unreadable=unreadable
                )
            self.save()
        return bool(self.changes)

    def check(self):
        """Observe the selected files and raise if this run has recorded any changes."""

        if self.observe():
            raise EnvironmentRevisionChanged(
                'Environment worktree changed during this run; stop before further generation/actions. See environment-revision.json'
            )

    def save(self):
        """Write the initial manifest, latest manifest, and accumulated changes when
        attached.
        """

        if self.path is not None:
            save_json(
                self.path,
                {
                    'started_at': self.started_at,
                    'checked_at': utc_now(),
                    'root': str(self.root),
                    'initial': self.start,
                    'latest': self.latest,
                    'changes': list(self.changes.values()),
                    'scope_patterns': list(self.PATTERNS),
                    'archive': 'environment-source/',
                    'isolation': False,
                },
            )

    def summary(self):
        """Return report fields for the fingerprints, changed files, and archive paths."""

        return {
            'initial_fingerprint': self.start['fingerprint'],
            'latest_fingerprint': self.latest['fingerprint'],
            'changed_during_run': bool(self.changes),
            'changed_files': sorted(self.changes),
            'file_count': len(self.start['files']),
            'manifest': 'environment-revision.json',
            'source_archive': 'environment-source/',
            'isolation': False,
        }

    def finalize(self):
        """Record one final observation and return the revision summary."""

        self.observe()
        self.save()
        return self.summary()
