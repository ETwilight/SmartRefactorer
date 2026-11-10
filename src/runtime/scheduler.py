"""Reuse generic queue admission while injecting the allocated worker entry."""

import os
import subprocess
from pathlib import Path
from runtime.capacity_scheduler import Scheduler as SharedScheduler, write


class Scheduler(SharedScheduler):
    def __init__(self, *args, allocated_worker, working_directory, **kwargs):
        super().__init__(*args, **kwargs)
        self.allocated_worker = allocated_worker
        self.working_directory = working_directory

    def _launch(self, job, slot):
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
