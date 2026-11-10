"""Task-stream environment lifecycle, including receipt-safe saved-world binding.

The method adapter supplies _resume; this layer owns concrete environment APIs.
"""

from adapters.environments.techmc.gateway import TechMCProvider


class TaskStreamProvider(TechMCProvider):
    def preflight(self, config):
        from environments.techmc.knowledge_backend import preflight

        preflight()
        if config.get('task_worlds') is False:
            raise ValueError('Task-stream methods require TaskStream with isolated task worlds')
        from environments.techmc.task_selection import TaskSelection
        from environments.techmc.task_stream import TaskStream

        options = config.get('options', {})
        if self._resume(config):
            return
        selection = (
            {'task_ids': options['task_ids']}
            if options.get('task_ids') is not None
            else config.get('task_selection')
        )
        TaskStream(
            TaskSelection(selection, world_type=config.get('world_type', 'void')),
            sequence=options.get('task_sequence', 'curriculum'),
            task_ids=options.get('task_ids'),
        )

    def configure_run(self, environment, config):
        options = config.get('options', {})
        resume = self._resume(config)
        if resume:
            if environment.task_stream is not None or environment.task_worlds is not None:
                raise ValueError('Resume environment already configured')
            environment.enable_task_stream(
                sequence=resume['stream_sequence'], task_ids=resume['stream_task_ids']
            )
            environment.enable_task_worlds()
            environment.resume_task_binding(resume['binding'])
            if resume.get('recovery_approval') is not None:
                environment.bind_recovery_approval(resume['recovery_approval'])
            return
        if environment.task_stream is None:
            environment.enable_task_stream(
                sequence=options.get('task_sequence', 'curriculum'),
                task_ids=options.get('task_ids'),
            )
        environment.enable_task_worlds()

    def connect(self, configuration):
        descriptor = getattr(self, '_verified_descriptor', None)
        if descriptor and descriptor.get('api_only_before_invocation'):
            from environments.techmc import Environment
            from environments.techmc.runtime_support import ActionSession
            from adapters.environments.techmc.gateway import EnvironmentGateway

            if (
                configuration.get('run_id') != descriptor['session_id']
                or configuration.get('resume_existing') is not True
            ):
                raise ValueError(
                    'API-only continuation configuration differs from verified source'
                )
            session = ActionSession(
                configuration['actor'],
                configuration['run_id'],
                client_required=False,
                server_port=configuration['server_port'],
                resume_existing=False,
            )
            return EnvironmentGateway(Environment(**dict(configuration), session=session))
        return super().connect(configuration)
