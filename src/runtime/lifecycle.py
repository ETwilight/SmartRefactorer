"""Experiment ownership and episode boundaries, independent of task domain."""

EPISODE_TERMINATION_GUIDE = '''Task completion and experiment termination are separate.
Finishing or submitting an episode ends only that task attempt. The experiment host
continues scheduling episodes until its limits, completion condition or operator stop.
A pass on one task does not finish the experiment. At the beginning of a new episode,
follow the environment's current task delivery and eligibility. The host owns the shared
session; do not close it. An unevaluated final message does not establish task success.
'''


class ManagedEnvironment:
    """Agents use handle(); only the trusted runner uses close()."""

    def __init__(self, environment):
        self.environment = environment

    def __getattr__(self, name):
        return getattr(self.environment, name)

    def handle(self, request):
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
        return self.environment.close()


def managed_environment(environment):
    return (
        environment
        if isinstance(environment, ManagedEnvironment)
        else ManagedEnvironment(environment)
    )
