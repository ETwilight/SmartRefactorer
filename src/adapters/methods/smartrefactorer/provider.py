"""Configure the TechMC task-stream provider for SR continuation records."""

import copy
from pathlib import Path
from adapters.environments.techmc.provider import TaskStreamProvider


class Provider(TaskStreamProvider):
    """TechMC provider with SR checkpoint and world-continuation lookup.

    TaskStreamProvider supplies task selection, world allocation, and lifecycle
    operations. _resume() adds the descriptor read from an SR source run. The
    descriptor is cached in _verified_descriptor for that source and server port.
    resume_readonly() classifies requests using the environment action catalog.

    Example, as called during worker preparation::

        provider = Provider()
        provider.preflight(config)

    This checks the task/environment settings and continuation source before the
    worker requests an environment slot.
    """

    def resume_readonly(self, request):
        """Return whether the environment classifies the request as a read-only operation."""

        from environments.techmc.actions.catalog import request_effect

        return request_effect(request) == 'read_only'

    def _resume(self, config):
        """Load or reuse the configured SR continuation descriptor for the selected server
        port.
        """

        source = config.get('options', {}).get('smartrefactorer_resume_from')
        if source is None:
            return None
        cached = getattr(self, '_verified_descriptor', None)
        if cached and cached['source_run'] == str(Path(source).resolve()):
            if config['port'] != cached['port']:
                raise ValueError('Continuation port changed')
            return copy.deepcopy(cached)
        from .world_resume import resume_descriptor
        from environments.techmc.contracts import ROOT

        value = resume_descriptor(
            Path(source).resolve(),
            root=ROOT,
            method='smartrefactorer',
            recovery_receipt=config.get('options', {}).get(
                'smartrefactorer_resume_recovery_receipt'
            ),
        )
        if config['port'] != value['port']:
            raise ValueError('Continuation port changed')
        self._verified_descriptor = copy.deepcopy(value)
        return copy.deepcopy(value)
