"""Track the evaluator player's position, game mode, and lifecycle throughout acceptance."""

import math
import time


class EvaluatorActor:
    """Check continuity of the task's required operating player.

    call accepts a server method and parameters and returns its unwrapped result.
    name, position, and dimension identify the expected actor and operating site.
    baseline and latest are dict | None, referencing the first verified status
    and most recent readback. samples counts successful status-query returns;
    last_sample is a monotonic timestamp used by the optional sampling throttle.

    status begins as inconclusive. sample() checks connected/alive flags,
    Survival mode, fake-player identity, position, and stable UUID/death/connection
    counters. An observed violation sets fail and raises ValueError. Missing
    telemetry or a read error sets inconclusive and raises RuntimeError. start()
    also checks the loaded site's collision geometry and support/fluid/flying
    condition. summary() returns the recorded status and initial/final readbacks.

    Example:
        actor = EvaluatorActor(call, name, position, dimension)
        actor.start()
        actor.sample()
        result = actor.summary()

    result reports actor continuity across the sampled measurement interval.
    The evaluator uses this status as one of its acceptance checks.
    """

    def __init__(self, call, name, position, dimension):
        """Bind the status-query callback and expected actor/site, with no initial samples."""

        self.call = call
        self.name = name
        self.position = position
        self.dimension = dimension
        self.baseline = None
        self.latest = None
        self.status = 'inconclusive'
        self.message = 'Not started'
        self.samples = 0
        self.last_sample = 0

    def fail(self, message):
        """Record an observed actor violation and raise ValueError with its message."""

        self.status = 'fail'
        self.message = message
        raise ValueError(message)

    def sample(self, force=True):
        """Read and check actor continuity, allowing a 50 ms sampling skip when force is
        false.
        """

        if not force and time.monotonic() - self.last_sample < 0.05:
            return
        try:
            state = self.call('fake_player.status', {'id': self.name})
        except Exception as exc:
            self.status = 'inconclusive'
            self.message = 'Evaluator actor readback unavailable: ' + str(exc)
            raise RuntimeError(self.message) from exc
        self.samples += 1
        self.last_sample = time.monotonic()
        self.latest = state
        if type(state.get('connected')) is not bool or type(state.get('alive')) is not bool:
            self.status = 'inconclusive'
            self.message = 'Evaluator actor lifecycle telemetry missing; update server bridge'
            raise RuntimeError(self.message)
        if not state['connected'] or not state['alive']:
            self.fail('Required evaluator actor disconnected or died during acceptance')
        if 'game_mode' not in state:
            self.status = 'inconclusive'
            self.message = 'Evaluator actor game-mode telemetry missing; update server bridge'
            raise RuntimeError(self.message)
        if state['game_mode'] != 'survival':
            self.fail(
                'Every evaluator actor, including an idle observer, must remain in Survival mode'
            )
        if not state.get('fake') or state.get('spectator'):
            self.fail('Required evaluator actor must be a non-spectator fake player')
        if (
            state.get('dimension') != self.dimension
            or len(state.get('position', [])) != 3
            or math.dist(state['position'], self.position) >= 1
        ):
            self.fail('Required evaluator actor left its declared operating position/dimension')
        for key in ('uuid', 'deaths', 'connection_serial'):
            if key not in state:
                self.status = 'inconclusive'
                self.message = 'Evaluator actor lifecycle counter unavailable: ' + key
                raise RuntimeError(self.message)
            if self.baseline is not None and state[key] != self.baseline[key]:
                self.fail('Evaluator actor lifecycle changed during acceptance: ' + key)
        if self.baseline is None:
            # baseline changes from None to the first verified status dict, retained for later
            # lifecycle comparisons.
            self.baseline = state
        self.status = 'pass'
        self.message = (
            'Required evaluator actor remained alive, connected and at its operating position'
        )
        return state

    def start(self):
        """Take the initial actor sample and check the declared operating site's loading and
        geometry.
        """

        self.sample()
        site = self.call(
            'observe.afk_site', {'position': self.position, 'dimension': self.dimension}
        )
        if not site.get('loaded'):
            self.status = 'inconclusive'
            self.message = 'AFK site is not loaded'
            raise RuntimeError(self.message)
        if not site.get('body_clear'):
            self.fail('Declared AFK position intersects block collision geometry')
        if (
            not site.get('has_support')
            and not site.get('fluid_at_feet')
            and not self.latest.get('flying')
        ):
            self.fail(
                'Declared AFK position has no support and the evaluator actor is not flying'
            )

    def summary(self):
        """Return continuity status, sample count, and the initial and latest actor
        readbacks.
        """

        return {
            'status': self.status,
            'message': self.message,
            'actor': self.name,
            'samples': self.samples,
            'initial': self.baseline,
            'final': self.latest,
            'automatic_protection': False,
            'automatic_respawn': False,
        }
