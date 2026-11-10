"""Opt-in SSH hosts keyed by LOCAL bridge port. No passwords or model credentials."""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[2] / "assets/techmc"
_cache = {}
_lock = threading.Lock()


def profile(port, root=None):
    root = Path(root or ROOT)
    config = Path(os.environ.get('TECHMC_REMOTE_CONFIG', root / 'runtime/remote-servers.json'))
    if not config.is_file():
        return None
    value = json.loads(config.read_text(encoding='utf-8'))['servers'].get(str(port))
    if value is None:
        return None
    value = dict(value)
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', value['ssh_host']) or value['ssh_host'].startswith(
        '-'
    ):
        raise ValueError('Invalid SSH host/alias')
    remote_root = PurePosixPath(value['remote_root'])
    if not remote_root.is_absolute() or '..' in remote_root.parts:
        raise ValueError('remote_root must be an absolute Linux path without ..')
    for key in ('remote_port', 'local_game_port', 'remote_game_port'):
        if not 1024 <= int(value[key]) <= 65535:
            raise ValueError('Invalid remote host port: ' + key)
    value['local_port'] = int(port)
    value['local_root'] = str(root)
    return value


class RemotePrelaunchRejected(RuntimeError):
    """Trusted remote ACK that this exact launch was rejected before Popen."""

    def __init__(self, acknowledgement):
        required = {
            'protocol',
            'action',
            'experiment_id',
            'instance_id',
            'launch_id',
            'outcome',
            'popen_called',
            'capacity_declined',
            'error',
        }
        ack = copy.deepcopy(acknowledgement)
        if (
            not isinstance(ack, dict)
            or set(ack) != required
            or ack['protocol'] != 'techmc.prelaunch_ack.v1'
            or ack['outcome'] != 'rejected'
            or ack['popen_called'] is not False
            or type(ack['capacity_declined']) is not bool
            or not isinstance(ack['action'], str)
            or any(
                ack[k] is not None and not isinstance(ack[k], str)
                for k in ('experiment_id', 'instance_id', 'launch_id')
            )
            or not isinstance(ack['error'], dict)
            or set(ack['error']) != {'type', 'message'}
            or not all(isinstance(v, str) for v in ack['error'].values())
        ):
            raise ValueError('Malformed remote prelaunch acknowledgement')
        self._acknowledgement = ack
        super().__init__(ack['error']['type'] + ': ' + ack['error']['message'])

    @property
    def acknowledgement(self):
        return copy.deepcopy(self._acknowledgement)

    @property
    def capacity_declined(self):
        return self._acknowledgement['capacity_declined'] is True

    def matches(self, *, action, experiment_id, instance_id, launch_id):
        return all(
            self._acknowledgement[k] == v
            for k, v in {
                'action': action,
                'experiment_id': experiment_id,
                'instance_id': instance_id,
                'launch_id': launch_id,
            }.items()
        )


def _raise_prelaunch_rejection(raw, *, action, experiment_id, instance_id, launch_id):
    try:
        ack = json.loads(raw)
    except (ValueError, UnicodeError):
        return
    if not isinstance(ack, dict) or ack.get('protocol') != 'techmc.prelaunch_ack.v1':
        return
    rejection = RemotePrelaunchRejected(ack)
    if not rejection.matches(
        action=action, experiment_id=experiment_id, instance_id=instance_id, launch_id=launch_id
    ):
        raise ValueError('Remote prelaunch acknowledgement belongs to another invocation')
    raise rejection


class RemoteHost:
    def __init__(self, settings):
        self.settings = settings
        self.local_port = int(settings['local_port'])
        self.token_file = (
            Path(settings['local_root'])
            / 'runtime/remote'
            / str(self.local_port)
            / 'bridge.token'
        )

    def ssh(self):
        command = [
            'ssh',
            '-o',
            'BatchMode=yes',
            '-o',
            'ConnectTimeout=10',
            '-o',
            'ForwardAgent=no',
            '-o',
            'StrictHostKeyChecking=yes',
            '-o',
            'ServerAliveInterval=15',
            '-o',
            'ServerAliveCountMax=3',
        ]
        if self.settings.get('identity_file'):
            command += ['-i', str(Path(self.settings['identity_file']).expanduser())]
        command += ['-p', str(self.settings.get('ssh_port', 22))]
        return command

    def execute(self, code, data=None, output=None, timeout=60):
        # Send source over stdin: no shell interpolation of paths or parameters.
        payload = {**self.settings, **(data or {})}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        script = (
            "import base64,json,os,pathlib,subprocess,sys,time\n"
            "cfg=json.loads(base64.b64decode('" + encoded + "'))\n"
            "root=pathlib.Path(cfg['remote_root'])\n" + code
        )
        result = subprocess.run(
            self.ssh() + [self.settings['ssh_host'], 'python3', '-'],
            input=script.encode(),
            stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(
                'Remote command failed: ' + result.stderr.decode(errors='replace')[-3000:]
            )
        return result.stdout

    def state(self, fresh=False):
        key = (self.settings['ssh_host'], self.settings['remote_root'], self.local_port)
        with _lock:
            cached = _cache.get(key)
            if not fresh and cached and time.monotonic() - cached[0] < 10:
                return dict(cached[1])
        raw = self.execute(
            "import socket\n"
            "p=root/'runtime/servers'/str(cfg['remote_port'])/'current.json'\n"
            "value=json.loads(p.read_text()) if p.exists() else {}\n"
            "if value:\n"
            "    value['supervisor_lock_exists']=(p.parent/'supervisor.lock').exists()\n"
            "    cmdline=pathlib.Path('/proc')/str(value.get('pid',0))/'cmdline'\n"
            "    expected=('-Dminecraft.agent.instance='+value['instance_id']).encode()\n"
            "    try: value['process_alive']=expected in cmdline.read_bytes().split(b'\\x00')\n"
            "    except FileNotFoundError: value['process_alive']=False\n"
            "    try:\n"
            "        with socket.create_connection(('127.0.0.1',int(cfg['remote_port'])),timeout=.3): value['api_listening']=True\n"
            "    except OSError: value['api_listening']=False\n"
            "    if value.get('status') in ('ready','starting') and not value['process_alive']:\n"
            "        value['state_note']='Recorded state may be stale; inspect process and supervisor lock before recovery'\n"
            "print(json.dumps(value))\n"
        )
        value = json.loads(raw)
        if value:
            if value['bridge_port'] != int(self.settings['remote_port']):
                raise ValueError('Remote state belongs to another bridge port')
            value.update(
                remote_bridge_port=value['bridge_port'],
                bridge_port=self.local_port,
                remote=True,
                ssh_host=self.settings['ssh_host'],
                game_address='127.0.0.1:' + str(self.settings['local_game_port']),
            )
        with _lock:
            _cache[key] = (time.monotonic(), value)
        return dict(value)

    def fetch_token(self):
        token = self.execute(
            "sys.stdout.buffer.write((root/'runtime/bridge.token').read_bytes())\n"
        )
        if not token.strip() or len(token) > 4096:
            raise ValueError('Invalid remote bridge token')
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_bytes(token)
        self.token_file.chmod(0o600)

    def verify_deployment(self, expected=None):
        """Hash actual remote assets, not just a receipt. Never starts a process."""
        if expected is None:
            if __package__:
                from .deployment import manifest
            else:
                from deployment import manifest
            expected = manifest(Path(self.settings['local_root']))
        raw = self.execute(
            "import hashlib\n"
            "missing=[]; changed=[]; unsafe=[]\n"
            "for name,expected in cfg['expected'].items():\n"
            "    relative=pathlib.PurePosixPath(name)\n"
            "    path=root/name\n"
            "    if relative.is_absolute() or '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):\n"
            "        unsafe.append(name); continue\n"
            "    if not path.is_file(): missing.append(name); continue\n"
            "    digest=hashlib.sha256()\n"
            "    with path.open('rb') as stream:\n"
            "        for block in iter(lambda:stream.read(1048576),b''): digest.update(block)\n"
            "    if digest.hexdigest()!=expected: changed.append(name)\n"
            "receipt=root/'runtime/deployment-manifest.json'\n"
            "recorded=json.loads(receipt.read_text()) if receipt.is_file() else {}\n"
            "extra=sorted(set(recorded)-set(cfg['expected']))\n"
            "print(json.dumps({'status':'matched' if not (missing or changed or unsafe or extra) else 'mismatch',\n"
            "                  'files_checked':len(cfg['expected']),'missing':missing,'changed':changed,'unsafe':unsafe,'obsolete':extra}))\n",
            {'expected': expected},
            timeout=120,
        )
        return json.loads(raw)

    def require_deployment(self):
        result = self.verify_deployment()
        if result['status'] != 'matched':
            raise RuntimeError(
                'Remote deployment differs from this environment; run tools/remote_server.py deploy before starting: '
                + json.dumps(result)
            )
        return result

    def retained_resume_proof(self, instance_id, experiment_id, owner_token):
        """Prove the owned target on remote storage, separately from slot admission."""
        raw = self.execute(
            "import hashlib\n"
            "sys.path.insert(0,str(root))\n"
            "from experiment_registry import retained_resume_target,_read_json_retry\n"
            "control=root/'runtime/servers'/str(cfg['remote_port'])\n"
            "value=retained_resume_target(root,cfg['remote_port'],cfg['instance_id'],cfg['experiment_id'],cfg['owner_token'])\n"
            "current=_read_json_retry(control/'current.json',missing_ok=True)\n"
            "owner=hashlib.sha256(cfg['owner_token'].strip().encode()).hexdigest()\n"
            "print(json.dumps({'verified':True,'instance':value,'current':current,'owner_hash':owner,'slot_admission_required':True}))\n",
            {
                'instance_id': instance_id,
                'experiment_id': experiment_id,
                'owner_token': owner_token,
            },
        )
        return json.loads(raw)

    def experiment_state(self):
        return json.loads(
            self.execute(
                "sys.path.insert(0,str(root))\nfrom experiment_registry import _read_json_retry\nprint(json.dumps(_read_json_retry(root/'runtime/experiments'/cfg['experiment_id']/'experiment.json')))\n",
                {'experiment_id': self.experiment_id},
            )
        )

    def control(self, action, arguments=(), *, launch_id=None):
        if action not in ('run', 'resume', 'stop', 'save', 'status'):
            raise ValueError('Invalid remote control action')
        if action in ('run', 'resume'):
            self.require_deployment()
        credential = getattr(self, 'owner_token_file', None) or os.environ.get(
            'MC_EXPERIMENT_TOKEN_FILE'
        )
        owner_token = Path(credential).read_text().strip() if credential else None
        experiment_id = getattr(self, 'experiment_id', None) or os.environ.get(
            'MC_EXPERIMENT_ID'
        )
        if action in ('run', 'resume') and not owner_token:
            raise PermissionError(
                'Start through tools/experiment.py, or provide MC_EXPERIMENT_ID and MC_EXPERIMENT_TOKEN_FILE'
            )
        values = list(arguments)
        index = values.index('--instance-id') if '--instance-id' in values else None
        instance_id = (
            values[index + 1] if index is not None and index + 1 < len(values) else None
        )
        raw = self.execute(
            "sys.path.insert(0,str(root))\n"
            "from experiment_registry import authenticate,credentials,write,HostLease,check_existing,host_policy,_read_json_retry,retained_resume_target\n"
            "from host_resources import admission,CapacityUnavailable\n"
            "import hashlib\n"
            "def require_owned(document):\n"
            "    authenticate(document,cfg['owner_token'])\n"
            "    if cfg['experiment_id'] and (document.get('experiment_id')!=cfg['experiment_id'] or document.get('owner_hash')!=hashlib.sha256((cfg['owner_token'] or '').strip().encode()).hexdigest()): raise PermissionError('Remote lifecycle control belongs to a different owner/experiment')\n"
            "command=['python3',str(root/'tools/void_server.py'),cfg['action'],'--port',str(cfg['remote_port'])]+cfg['arguments']\n"
            "if cfg['action'] in ('run','resume'):\n"
            "    try:\n"
            "        control=root/'runtime/servers'/str(cfg['remote_port']); control.mkdir(parents=True,exist_ok=True)\n"
            "        p=control/'current.json'\n"
            "        if cfg['action']=='resume':\n"
            "            if cfg['instance_id'] is not None: retained_resume_target(root,cfg['remote_port'],cfg['instance_id'],cfg['experiment_id'],cfg['owner_token'])\n"
            "            elif p.exists(): require_owned(_read_json_retry(p))\n"
            "            else: raise ValueError('Resume requires an explicit retained instance or owned current')\n"
            "        with HostLease(root):\n"
            "            check_existing(root,cfg['remote_port'],cfg['action']=='resume',experiment_id=cfg['experiment_id'],max_servers=host_policy(root))\n"
            "            values=cfg['arguments']; heap=int(values[values.index('--heap-mb')+1]) if '--heap-mb' in values else 2048\n"
            "            admission(root,heap)\n"
            "        owner_path,owner_hash=credentials(root,cfg['experiment_id'],cfg['owner_token'])\n"
            "        command+=['--experiment-id',cfg['experiment_id']]\n"
            "        environment=os.environ.copy()\n"
            "        if cfg.get('launch_id'): environment['MC_EXPERIMENT_LAUNCH_ID']=cfg['launch_id']\n"
            "        else: environment.pop('MC_EXPERIMENT_LAUNCH_ID',None)\n"
            "        log=(control/'launcher.log').open('ab')\n"
            "    except Exception as exc:\n"
            "        message=str(exc).replace(cfg['owner_token'] or '<no-owner-token>','<redacted>')\n"
            "        print(json.dumps({'protocol':'techmc.prelaunch_ack.v1','action':cfg['action'],'experiment_id':cfg['experiment_id'],'instance_id':cfg['instance_id'],'launch_id':cfg['launch_id'],'outcome':'rejected','popen_called':False,'capacity_declined':isinstance(exc,CapacityUnavailable),'error':{'type':type(exc).__name__,'message':message}}))\n"
            "    else:\n"
            "        with log:\n"
            "            process=subprocess.Popen(command,cwd=root,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)\n"
            "        print(json.dumps({'launcher_pid':process.pid}))\n"
            "else:\n"
            "    p=root/'runtime/servers'/str(cfg['remote_port'])/'current.json'\n"
            "    if cfg['action'] in ('stop','save') and p.exists(): require_owned(_read_json_retry(p))\n"
            "    environment=os.environ.copy()\n"
            "    if p.exists():\n"
            "        state=json.loads(p.read_text())\n"
            "        if state.get('owner_token_file'): environment['MC_EXPERIMENT_TOKEN_FILE']=state['owner_token_file']\n"
            "    result=subprocess.run(command,cwd=root,capture_output=True,env=environment)\n"
            "    sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr); sys.exit(result.returncode)\n",
            {
                'action': action,
                'arguments': values,
                'owner_token': owner_token,
                'experiment_id': experiment_id,
                'launch_id': launch_id,
                'instance_id': instance_id,
            },
            timeout=150,
        )
        if action in ('run', 'resume'):
            _raise_prelaunch_rejection(
                raw,
                action=action,
                experiment_id=experiment_id,
                instance_id=instance_id,
                launch_id=launch_id,
            )
        self.state(fresh=True)
        return raw.decode()

    def task_world_lease(self, release=False, *, launch_id=None, instance_id=None):
        credential = getattr(self, 'owner_token_file', None) or os.environ.get(
            'MC_EXPERIMENT_TOKEN_FILE'
        )
        if not credential:
            raise PermissionError('Task-world lease requires experiment ownership')
        token = Path(credential).read_text().strip()
        experiment_id = getattr(self, 'experiment_id', None) or os.environ.get(
            'MC_EXPERIMENT_ID'
        )
        raw = self.execute(
            "sys.path.insert(0,str(root))\nfrom experiment_registry import HostLease,task_world_lease\n"
            "from host_resources import CapacityUnavailable\n"
            "if cfg['release']:\n"
            "    with HostLease(root,timeout=5): result=task_world_lease(root,cfg['remote_port'],cfg['experiment_id'],cfg['owner_token'],release=True)\n"
            "    print(json.dumps(result))\n"
            "else:\n"
            "    try:\n"
            "        with HostLease(root,timeout=5): result=task_world_lease(root,cfg['remote_port'],cfg['experiment_id'],cfg['owner_token'],release=False)\n"
            "    except Exception as exc:\n"
            "        message=str(exc).replace(cfg['owner_token'] or '<no-owner-token>','<redacted>')\n"
            "        print(json.dumps({'protocol':'techmc.prelaunch_ack.v1','action':'task_world_lease.acquire','experiment_id':cfg['experiment_id'],'instance_id':cfg['instance_id'],'launch_id':cfg['launch_id'],'outcome':'rejected','popen_called':False,'capacity_declined':isinstance(exc,CapacityUnavailable),'error':{'type':type(exc).__name__,'message':message}}))\n"
            "    else: print(json.dumps(result))\n",
            {
                'experiment_id': experiment_id,
                'owner_token': token,
                'release': release,
                'launch_id': launch_id,
                'instance_id': instance_id,
            },
        )
        if not release:
            _raise_prelaunch_rejection(
                raw,
                action='task_world_lease.acquire',
                experiment_id=experiment_id,
                instance_id=instance_id,
                launch_id=launch_id,
            )
        return json.loads(raw)

    def checkpoint(self, label, archive):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', label):
            raise ValueError('Invalid checkpoint label')
        archive = Path(archive)
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_suffix('.part')
        try:
            with temporary.open('wb') as output:
                self.execute(
                    "import tempfile,zipfile,secrets\n"
                    "control=root/'runtime/servers'/str(cfg['remote_port'])\n"
                    "state=json.loads((control/'current.json').read_text())\n"
                    "if state.get('status')!='ready': raise RuntimeError('Remote server is not ready')\n"
                    "save_label=cfg['label']+'-'+secrets.token_hex(8)\n"
                    "request={'instance_id':state['instance_id'],'action':'save','label':save_label}\n"
                    "pending=control/'save-request.tmp'; pending.write_text(json.dumps(request)); pending.replace(control/'save-request.json')\n"
                    "deadline=time.monotonic()+45\n"
                    "while True:\n"
                    "    confirmation=control/'save-confirmation.json'\n"
                    "    ack=json.loads(confirmation.read_text()) if confirmation.exists() else {}\n"
                    "    if ack.get('instance_id')==state['instance_id'] and ack.get('label')==save_label: break\n"
                    "    if time.monotonic()>deadline: raise TimeoutError('Remote save not confirmed')\n"
                    "    time.sleep(.2)\n"
                    "world=pathlib.Path(state['world']).resolve()\n"
                    "if not world.is_relative_to((control/'instances').resolve()): raise ValueError('Invalid world path')\n"
                    "with tempfile.TemporaryFile() as snapshot:\n"
                    "    with zipfile.ZipFile(snapshot,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as bundle:\n"
                    "        for path in world.rglob('*'):\n"
                    "            if path.is_symlink(): raise ValueError('Linked world assets forbidden')\n"
                    "            if path.is_file() and path.name!='session.lock': bundle.write(path,path.relative_to(world))\n"
                    "    snapshot.seek(0)\n"
                    "    import shutil; shutil.copyfileobj(snapshot,sys.stdout.buffer)\n",
                    {'label': label},
                    output=output,
                    timeout=180,
                )
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)

    def tunnel(self):
        forwards = [
            (self.local_port, int(self.settings['remote_port'])),
            (int(self.settings['local_game_port']), int(self.settings['remote_game_port'])),
        ]
        if len({x[0] for x in forwards}) != len(forwards):
            raise ValueError('Bridge and game forwarding ports must differ')
        # Refuse occupied listeners rather than silently using another local server.
        for local, _ in forwards:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', local))
        command = self.ssh() + ['-N', '-T', '-o', 'ExitOnForwardFailure=yes']
        for local, remote in forwards:
            command += ['-L', f'127.0.0.1:{local}:127.0.0.1:{remote}']
        return command + [self.settings['ssh_host']]


def for_port(port, root=None):
    settings = profile(port, root)
    return RemoteHost(settings) if settings else None
