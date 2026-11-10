"""Reuse shared provider lifecycle with SmartRefactorer-owned continuation validation."""

import copy
from pathlib import Path
from adapters.environments.techmc.provider import TaskStreamProvider


class Provider(TaskStreamProvider):

    def resume_readonly(self, request):
        from environments.techmc.actions.catalog import request_effect

        return request_effect(request) == 'read_only'

    def _resume(self, config):
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
