"""Pin code for a run and stop before another paid call after source drift."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from audit.control import RunStopped

ACTIVE_GUARD = ContextVar('experiment_source_guard', default=None)


class SourceGuard:
    def __init__(self, paths):
        self.stats = {
            Path(p): (Path(p).stat().st_mtime_ns, Path(p).stat().st_size) for p in paths
        }

    def __call__(self):
        for path, original in self.stats.items():
            try:
                current = (path.stat().st_mtime_ns, path.stat().st_size)
            except OSError:
                current = None
            if current != original:
                raise RunStopped(
                    'runtime_revision_changed',
                    'Pinned runtime or method source changed: ' + str(path),
                )


@contextmanager
def pin_source(guard):
    token = ACTIVE_GUARD.set(guard)
    try:
        yield
    finally:
        ACTIVE_GUARD.reset(token)
