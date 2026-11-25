"""Launch the shared capacity queue with a method-specific allocation worker."""

import os
import subprocess
from pathlib import Path
from runtime.capacity_scheduler import Scheduler as SharedScheduler, write


class Scheduler(SharedScheduler):
    """Select the worker module and working directory for the shared scheduler.

    allocated_worker is the importable module started by each job's interpreter.
    working_directory is the child process's cwd. The inherited queue tracks
    job dictionaries, running process/lease pairs, capacity, and completion.

    Example:
        scheduler = Scheduler(
            folder,
            provider,
            jobs,
            allocated_worker=worker_module,
            working_directory=research_root,
        )
        finished = scheduler.run(provider.slots(ports))

    Each admitted job runs worker_module with a generated launch.json containing
    its assigned environment port. finished holds the updated queue records.
    """

    def __init__(self, *args, allocated_worker, working_directory, **kwargs):
        """Initialize the queue and select its allocation worker and child-process cwd."""

        super().__init__(*args, **kwargs)
        self.allocated_worker = allocated_worker
        self.working_directory = working_directory

    def _launch(self, job, slot):
        """Write the assigned port into launch.json and start the selected worker module."""

        config = {
            **job['configuration'],
            'port': slot['port'],
            'environment_options': {
                **job['configuration'].get('environment_options', {}),
                'server_port': slot['port'],
            },
        }
        path = Path(job['run_dir']) / 'launch.json'
        write(path, config)
        with (path.parent / 'launcher.log').open('ab') as log:
            return subprocess.Popen(
                [
                    job['python'],
                    '-X',
                    'utf8',
                    '-m',
                    self.allocated_worker,
                    '--config',
                    str(path.resolve()),
                ],
                cwd=self.working_directory,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
