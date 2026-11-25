"""Configure task-stream environments and reconnect verified saved task/world bindings."""

from adapters.environments.techmc.gateway import TechMCProvider


class TaskStreamProvider(TechMCProvider):
    """Apply task-stream setup to methods using the shared TechMC provider.

    This subclass checks the selected arrivals, attaches a TaskStream and
    retained task-world manager, and restores verified saved bindings. A method
    subclass can override _resume() for its own checkpoint format; the inherited
    provider handles allocation, connection options, scheduling, and auditing.

    A method's _verified_descriptor may identify an API-only checkpoint created
    before primitive invocation. connect() then constructs an ActionSession
    handle using the verified session ID and original server port, while the
    Environment restores its saved API state.

    Example:
        provider = TaskStreamProvider()
        gateway = provider.connect(provider.connection_options(config))
        provider.configure_run(gateway, config)
        gateway.close()

    The gateway is configured for sequential task visits and retained task
    worlds. Method-specific subclasses use this lifecycle with their own
    continuation descriptor implementation.
    """

    def preflight(self, config):
        """Check knowledge dependencies and validate the configured arrival sequence or
        continuation descriptor.
        """

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
        """Attach task delivery and worlds, or restore their verified saved task binding."""

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
        """Connect through the base provider or reconstruct the session handle for a verified
        API-only checkpoint.
        """

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
