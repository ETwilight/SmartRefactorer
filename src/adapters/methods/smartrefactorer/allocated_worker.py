"""Supply SR entrypoints to generic allocation/heartbeat/final-save ownership."""

from pathlib import Path
from runtime.allocation import main as run_allocated
from .worker import preflight
from .registry import ENVIRONMENTS, resolve


def main(argv=None):
    return run_allocated(
        argv,
        preflight=preflight,
        provider_for=lambda name: resolve(ENVIRONMENTS, name),
        worker_module='adapters.methods.smartrefactorer.worker',
        working_directory=Path(__file__).resolve().parents[3],
    )


if __name__ == '__main__':
    from runtime.quiet_processes import enable_background_children

    enable_background_children()
    raise SystemExit(main())
