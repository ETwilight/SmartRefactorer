"""SR launch configuration for the shared generic scheduler."""

from pathlib import Path
from runtime.scheduler import Scheduler as RuntimeScheduler, write


class Scheduler(RuntimeScheduler):
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            allocated_worker='adapters.methods.smartrefactorer.allocated_worker',
            working_directory=Path(__file__).resolve().parents[3],
            **kwargs
        )
