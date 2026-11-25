"""Track source-file metadata and expose a call-boundary change check."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from audit.control import RunStopped

ACTIVE_GUARD = ContextVar('experiment_source_guard', default=None)


class SourceGuard:
    """Compare the current source metadata with the values captured at run setup.

    stats maps each Path to its modification time in nanoseconds and file size in
    bytes. Calling the object reads those values again. A missing or changed file
    raises RunStopped with runtime_revision_changed so the runner can save its state.

    Example, from worker setup::

        guard = SourceGuard(source_paths)
        with pin_source(guard):
            report = plugin.run(context)

    The model transport can call the installed guard at its next call boundary.
    """

    def __init__(self, paths):
        """Capture modification times and sizes for the supplied source files."""

        self.stats = {
            Path(p): (Path(p).stat().st_mtime_ns, Path(p).stat().st_size) for p in paths
        }

    def __call__(self):
        """Compare file metadata and raise RunStopped when a captured file has changed."""

        for path, original in self.stats.items():
            try:
                current = (path.stat().st_mtime_ns, path.stat().st_size)
            except OSError:
                # current: tuple[int, int] | None; a failed stat is represented by None for
                # comparison.
                current = None
            if current != original:
                raise RunStopped(
                    'runtime_revision_changed',
                    'Pinned runtime or method source changed: ' + str(path),
                )


@contextmanager
def pin_source(guard):
    """Install the source check in the current context and restore the previous one on exit."""

    token = ACTIVE_GUARD.set(guard)
    try:
        yield
    finally:
        ACTIVE_GUARD.reset(token)
