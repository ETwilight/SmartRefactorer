"""Archive public environment interfaces and reject worktree drift during a run.

This is a source/manifest guard, not process isolation. Runtime worlds, tokens,
credentials, derived indexes and large Wiki/game-source archives are excluded.
"""

import hashlib
from pathlib import Path
from runtime.context.codec import compact_json
from audit.recorder import save_json, utc_now


class EnvironmentRevisionChanged(RuntimeError):
    pass


class EnvironmentRevision:
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
        paths = {p for pattern in self.PATTERNS for p in self.root.glob(pattern) if p.is_file()}
        files = {}
        contents = {}
        named = {path.relative_to(self.root).as_posix(): path for path in paths}
        # Installed assets retain their old location; executable environment
        # code has a separate owner. Pin both, without unsafe '..' archive keys.
        source = self.root.parent.parent / 'environments/techmc'
        if self.root.name == 'techmc' and self.root.parent.name == 'assets' and source.is_dir():
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
        self.path = Path(path)
        self.recorder = recorder
        archive = self.path.parent / 'environment-source'
        for name, data in self.original_bytes.items():
            target = archive / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.original_bytes = {}
        self.save()
        self.check()

    def observe(self):
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
        if self.observe():
            raise EnvironmentRevisionChanged(
                'Environment worktree changed during this run; stop before further generation/actions. See environment-revision.json'
            )

    def save(self):
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
        self.observe()
        self.save()
        return self.summary()
