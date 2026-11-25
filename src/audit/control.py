"""Read cooperative operator controls for a running experiment."""

import json
from pathlib import Path


class RunStopped(BaseException):
    """Carry a run outcome label through model and environment call boundaries.

    reason becomes the runner's status; the exception message records its detail.

    Example:
        raise RunStopped('operator_stop', 'Operator requested a cooperative stop')

    The outer runner records this stop and closes the active episode.
    """

    def __init__(self, reason, message):
        """Store the stop reason and exception message."""

        super().__init__(message)
        self.reason = reason


class RunControl:
    """Track the operator's stop request and effective episode cap.

    path names the JSON control file. initial_cap is the caller's maximum, or None
    for an uncapped run. cap retains the lowest accepted limit; stop latches True.
    last stores accepted control data and error retains a parse/read failure.

    Example:
        control = RunControl(folder / 'control.json', max_rounds)
        control.check(recorder)

    This refreshes the operator file and checks the active recorder against it.
    """

    def __init__(self, path, max_rounds):
        """Initialize the control path, caller cap, and latched control state."""

        self.path = Path(path)
        self.initial_cap = max_rounds
        self.cap = max_rounds
        self.stop = False
        self.last = None
        self.error = None

    def refresh(self):
        """Read control JSON, lower the effective cap, and latch stop or error state."""

        try:
            if self.path.exists():
                raw = self.path.read_bytes()
                if len(raw) > 65536:
                    raise ValueError('Control file exceeds 64 KiB')
                data = json.loads(raw.decode('utf-8-sig'))
                if not isinstance(data, dict) or set(data) - {'max_rounds', 'stop', 'reason'}:
                    raise ValueError('Unknown control fields')
                cap = data.get('max_rounds', self.cap)
                if cap is not None and (
                    type(cap) is not int
                    or cap < 0
                    or (self.initial_cap is not None and cap > self.initial_cap)
                ):
                    raise ValueError(
                        'max_rounds must be a nonnegative integer within any initial run cap'
                    )
                if type(data.get('stop', False)) is not bool:
                    raise ValueError('stop must be boolean')
                if 'reason' in data and not isinstance(data['reason'], str):
                    raise ValueError('reason must be text')
                if cap is not None:
                    self.cap = cap if self.cap is None else min(self.cap, cap)
                self.stop = self.stop or data.get('stop', False)
                self.last = data
        except (OSError, ValueError, UnicodeError) as exc:
            self.error = str(exc)
        if self.error:
            raise RunStopped(
                'operator_control_error',
                'Invalid operator control; further calls blocked: ' + self.error,
            )
        return self.cap

    def check(self, recorder):
        """Refresh controls and raise a run stop at the requested stop or episode boundary."""

        self.refresh()
        if self.stop:
            raise RunStopped('operator_stop', 'Operator requested a cooperative stop')
        if self.cap is not None and (
            (recorder.round is not None and recorder.round['round'] > self.cap)
            or (recorder.round is None and len(recorder.rounds) >= self.cap)
        ):
            raise RunStopped(
                'operator_round_cap_reached', 'Effective round cap reached: ' + str(self.cap)
            )

    def snapshot(self):
        """Return JSON-ready initial, effective, and last-read control state."""

        return {
            'file': str(self.path),
            'initial_max_rounds': self.initial_cap,
            'effective_max_rounds': self.cap,
            'stop': self.stop,
            'last_control': self.last,
            'error': self.error,
        }
