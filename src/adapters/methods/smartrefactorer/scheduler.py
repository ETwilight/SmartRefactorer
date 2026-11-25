"""Select the SR allocated worker for the shared batch scheduler."""

from pathlib import Path
from runtime.scheduler import Scheduler as RuntimeScheduler, write


class Scheduler(RuntimeScheduler):
    """Run queued jobs through the SR allocation worker.

    The shared scheduler supplies queue state and capacity slots. allocated_worker
    names the SR worker module, and working_directory points to research so its
    packages are importable by each launched process. Other scheduler arguments
    are forwarded to the shared constructor.

    Example, using the batch directory, provider, and prepared jobs::

        scheduler = Scheduler(batch_directory, provider, jobs)

    Workers launched by this scheduler use the SR environment and method setup.
    """

    def __init__(self, *args, **kwargs):
        """Set the SR worker module and research working directory for queued launches."""

        super().__init__(
            *args,
            allocated_worker='adapters.methods.smartrefactorer.allocated_worker',
            working_directory=Path(__file__).resolve().parents[3],
            **kwargs
        )
