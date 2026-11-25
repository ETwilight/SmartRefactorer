"""Keep environment-session cleanup with the runner while task attempts share the session."""

EPISODE_TERMINATION_GUIDE = '''Task completion and experiment termination are separate.
Finishing or submitting an episode ends only that task attempt. The experiment host
continues scheduling episodes until its limits, completion condition or operator stop.
A pass on one task does not finish the experiment. At the beginning of a new episode,
follow the environment's current task delivery and eligibility. The host owns the shared
session; do not close it. An unevaluated final message does not establish task success.
'''


class ManagedEnvironment:
    """Forward environment operations while the runner owns session cleanup.

    environment is the wrapped port. Attribute access and ordinary handle() calls
    are forwarded to it. A public session.close request receives an error response;
    the runner closes the underlying port through this object's close() method.

    Example, around a runner's task loop::

        managed = managed_environment(environment)
        feedback = managed.handle(request)
        managed.close()

    The public request uses the shared session, and the runner closes it when done.
    """

    def __init__(self, environment):
        """Keep the environment port whose session the runner owns."""

        self.environment = environment

    def __getattr__(self, name):
        """Forward an attribute lookup to the wrapped environment."""

        return getattr(self.environment, name)

    def handle(self, request):
        """Forward public operations and return the runner-owned-session response for
        session.close.
        """

        if request.get('op') == 'session.close':
            session = getattr(self.environment, 'session', None)
            return {
                'protocol_version': '1.0',
                'request_id': request.get('request_id', 'session-close'),
                'session_id': getattr(session, 'run_id', 'unavailable'),
                'status': 'error',
                'result': None,
                'error': {
                    'type': 'ExperimentLifecycleManaged',
                    'message': 'The experiment runner owns this session. Finish the current episode through the method interface; this does not terminate the experiment.',
                },
            }
        return self.environment.handle(request)

    def close(self):
        """Close the wrapped environment through the runner interface."""

        return self.environment.close()


def managed_environment(environment):
    """Return the existing managed wrapper or wrap the supplied environment."""

    return (
        environment
        if isinstance(environment, ManagedEnvironment)
        else ManagedEnvironment(environment)
    )
