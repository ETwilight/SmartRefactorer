"""Cooperative operator controls, checked before every paid generation."""
import json
from pathlib import Path


class RunStopped(BaseException):
    def __init__(self,reason,message):
        super().__init__(message);self.reason=reason


class RunControl:
    def __init__(self,path,max_rounds):
        self.path=Path(path);self.initial_cap=max_rounds;self.cap=max_rounds;self.stop=False
        self.last=None;self.error=None

    def refresh(self):
        try:
            if self.path.exists():
                raw=self.path.read_bytes()
                if len(raw)>65536:raise ValueError('Control file exceeds 64 KiB')
                data=json.loads(raw.decode('utf-8-sig'))
                if not isinstance(data,dict) or set(data)-{'max_rounds','stop','reason'}:raise ValueError('Unknown control fields')
                cap=data.get('max_rounds',self.cap)
                if cap is not None and (type(cap) is not int or cap<0 or (self.initial_cap is not None and cap>self.initial_cap)):
                    raise ValueError('max_rounds must be a nonnegative integer within any initial run cap')
                if type(data.get('stop',False)) is not bool:raise ValueError('stop must be boolean')
                if 'reason' in data and not isinstance(data['reason'],str):raise ValueError('reason must be text')
                if cap is not None:self.cap=cap if self.cap is None else min(self.cap,cap)
                self.stop=self.stop or data.get('stop',False);self.last=data
        except (OSError,ValueError,UnicodeError) as exc:
            self.error=str(exc)
        if self.error:raise RunStopped('operator_control_error','Invalid operator control; further calls blocked: '+self.error)
        return self.cap

    def check(self,recorder):
        self.refresh()
        if self.stop:raise RunStopped('operator_stop','Operator requested a cooperative stop')
        if self.cap is not None and ((recorder.round is not None and recorder.round['round']>self.cap) or (recorder.round is None and len(recorder.rounds)>=self.cap)):
            raise RunStopped('operator_round_cap_reached','Effective round cap reached: '+str(self.cap))

    def snapshot(self):
        return {'file':str(self.path),'initial_max_rounds':self.initial_cap,'effective_max_rounds':self.cap,
                'stop':self.stop,'last_control':self.last,'error':self.error}
