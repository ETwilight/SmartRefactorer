"""Observe required evaluator actors; never protect, move, heal or respawn them."""

import math
import time


class EvaluatorActor:
    def __init__(self, call, name, position, dimension):
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
        self.status = 'fail'
        self.message = message
        raise ValueError(message)

    def sample(self, force=True):
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
            self.baseline = state
        self.status = 'pass'
        self.message = (
            'Required evaluator actor remained alive, connected and at its operating position'
        )
        return state

    def start(self):
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
