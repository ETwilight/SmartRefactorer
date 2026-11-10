"""Deploy and control an SSH-only MC host. LLMs, keys and evaluators stay local."""

from __future__ import annotations
from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[3] / "assets/techmc"
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.techmc.remote_host import for_port
from environments.techmc.task_selection import add_selection_arguments, selection_from_args


def package(destination):
    """Package only portable server assets; never copy worlds, tokens or API keys."""
    from environments.techmc.deployment import assets, manifest
    import io

    entries = assets(ROOT)
    hashes = manifest(ROOT, entries)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, 'w:gz') as bundle:
        for name, source in sorted(entries.items()):
            if isinstance(source, bytes):
                info = tarfile.TarInfo(name)
                info.size = len(source)
                bundle.addfile(info, io.BytesIO(source))
            else:
                bundle.add(source, arcname=name, recursive=False)
        receipt = json.dumps(hashes, indent=2).encode('utf-8')
        info = tarfile.TarInfo('runtime/deployment-manifest.json')
        info.size = len(receipt)
        bundle.addfile(info, io.BytesIO(receipt))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def deploy(remote):
    destination = ROOT / 'runtime/remote/deploy/server.tar.gz'
    checksum = package(destination)
    remote.execute("(root/'runtime/deploy').mkdir(parents=True,exist_ok=True)\n")
    command = [
        'scp',
        '-o',
        'BatchMode=yes',
        '-o',
        'ConnectTimeout=10',
        '-o',
        'ForwardAgent=no',
        '-o',
        'StrictHostKeyChecking=yes',
        '-P',
        str(remote.settings.get('ssh_port', 22)),
    ]
    if remote.settings.get('identity_file'):
        command += ['-i', str(Path(remote.settings['identity_file']).expanduser())]
    command += [
        str(destination),
        remote.settings['ssh_host']
        + ':'
        + remote.settings['remote_root']
        + '/runtime/deploy/server.tar.gz',
    ]
    subprocess.run(command, check=True)
    raw = remote.execute(
        "import hashlib,tarfile,secrets\n"
        "os.umask(0o077)\n"
        "policy=root/'runtime/host-policy.json'\n"
        "policy.write_text(json.dumps({'max_servers':1},indent=2))\n"
        "for p in (root/'runtime/servers').glob('*/current.json'):\n"
        "    if json.loads(p.read_text()).get('status') in ('ready','starting'): raise RuntimeError('Stop remote servers before deploying')\n"
        "archive=root/'runtime/deploy/server.tar.gz'\n"
        "if hashlib.sha256(archive.read_bytes()).hexdigest()!=cfg['sha256']: raise ValueError('Deployment archive checksum mismatch')\n"
        "with tarfile.open(archive) as bundle:\n"
        "    for member in bundle.getmembers():\n"
        "        if not member.isfile() or member.issym() or member.islnk() or not (root/member.name).resolve().is_relative_to(root.resolve()): raise ValueError('Unsafe deployment entry')\n"
        "    bundle.extractall(root,filter='data')\n"
        "manifest=json.loads((root/'runtime/deployment-manifest.json').read_text())\n"
        "for name,checksum in manifest.items():\n"
        "    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=checksum: raise ValueError('Deployed asset checksum mismatch: '+name)\n"
        "token=root/'runtime/bridge.token'\n"
        "if not token.exists(): token.write_text(secrets.token_hex(32))\n"
        "token.chmod(0o600)\n"
        "import jsonschema\n"
        "sys.path.insert(0,str(root))\n"
        "from environments.techmc.task_catalog import list_tasks\n"
        "task_count=len(list_tasks())\n"
        "java=json.loads((root/'runtime/java.json').read_text())['java']\n"
        "subprocess.run([java,'-version'],check=True,stdout=sys.stderr)\n"
        "print(json.dumps({'status':'deployed','files_verified':len(manifest),'task_count':task_count,'root':str(root)}))\n",
        {'sha256': checksum},
        timeout=180,
    )
    remote.fetch_token()
    print(raw.decode(), end='')
    result = remote.require_deployment()
    print(json.dumps(result))


def install_runtime(remote):
    """Install official Linux Java under the user's directory, without sudo."""
    print(
        remote.execute(
            "import hashlib,tarfile,importlib.util\n"
            "if importlib.util.find_spec('jsonschema') is None:\n"
            "    raise RuntimeError('Administrator command required: sudo apt-get install python3-jsonschema')\n"
            "if os.uname().machine!='x86_64': raise RuntimeError('Installer requires an x86_64 host')\n"
            "root.mkdir(parents=True,exist_ok=True)\n"
            "url='https://api.adoptium.net/v3/assets/latest/25/hotspot?architecture=x64&image_type=jdk&os=linux&vendor=eclipse'\n"
            "release=json.loads(subprocess.check_output(['curl','-fsSL','--retry','3','--max-time','60',url]))[0]\n"
            "package=release['binary']['package']\n"
            "downloads=root/'runtime/downloads'; downloads.mkdir(parents=True,exist_ok=True)\n"
            "archive=downloads/package['name']\n"
            "if not archive.exists(): subprocess.run(['curl','-fLsS','--retry','3','--max-time','600',package['link'],'-o',str(archive)],check=True)\n"
            "if archive.stat().st_size!=package['size'] or hashlib.sha256(archive.read_bytes()).hexdigest()!=package['checksum']: raise ValueError('Official Java checksum mismatch')\n"
            "destination=root/'runtime/java'; destination.mkdir(parents=True,exist_ok=True)\n"
            "java=destination/release['release_name']/'bin/java'\n"
            "if not java.is_file():\n"
            "    with tarfile.open(archive) as bundle: bundle.extractall(destination,filter='data')\n"
            "subprocess.run([str(java),'-version'],check=True,stdout=sys.stderr)\n"
            "(root/'runtime/java.json').write_text(json.dumps({'java':str(java),'release':release},indent=2))\n"
            "print(json.dumps({'status':'runtime_installed','java':str(java),'publisher_sha256':package['checksum']}))\n",
            timeout=900,
        ).decode(),
        end='',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'action',
        choices=[
            'configure',
            'install',
            'deploy',
            'check',
            'run',
            'resume',
            'stop',
            'save',
            'status',
            'tunnel',
            'logs',
        ],
    )
    parser.add_argument(
        '--port',
        type=int,
        default=18776,
        help='LOCAL bridge port; pass this port to every local workflow',
    )
    parser.add_argument('--host', help='SSH user@host or existing SSH config alias')
    parser.add_argument('--identity-file', type=Path)
    parser.add_argument('--ssh-port', type=int, default=22)
    parser.add_argument('--remote-root', default='/home/etwilight/techmc_env')
    parser.add_argument('--remote-port', type=int, default=8776)
    parser.add_argument('--local-game-port', type=int, default=25576)
    parser.add_argument('--remote-game-port', type=int, default=25566)
    parser.add_argument(
        '--heap-mb',
        type=int,
        default=2048,
        help='Single-server default; explicitly use 1024 when testing two servers',
    )
    parser.add_argument('--processors', type=int, default=1)
    parser.add_argument('--world-type', choices=['void', 'natural'], default='void')
    parser.add_argument('--seed', default='-7160105646644042722')
    parser.add_argument(
        '--background', action='store_true', help='Keep SSH tunnel running after this CLI exits'
    )
    add_selection_arguments(parser)
    args = parser.parse_args()
    if args.action == 'configure':
        if not args.host:
            parser.error('configure requires --host')
        path = Path(
            os.environ.get('TECHMC_REMOTE_CONFIG', ROOT / 'runtime/remote-servers.json')
        )
        settings = {
            'ssh_host': args.host,
            'ssh_port': args.ssh_port,
            'remote_root': args.remote_root,
            'remote_port': args.remote_port,
            'local_game_port': args.local_game_port,
            'remote_game_port': args.remote_game_port,
        }
        if args.identity_file:
            settings['identity_file'] = str(args.identity_file.expanduser().resolve())
        config = (
            json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'servers': {}}
        )
        config['servers'][str(args.port)] = settings
        # Validate before writing; config includes paths, never key contents.
        if not 1024 <= args.port <= 65535:
            parser.error('Invalid local bridge port')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(config, indent=2), encoding='utf-8')
        from environments.techmc.remote_host import profile

        old = os.environ.get('TECHMC_REMOTE_CONFIG')
        try:
            os.environ['TECHMC_REMOTE_CONFIG'] = str(temporary)
            profile(args.port)
        finally:
            if old is None:
                os.environ.pop('TECHMC_REMOTE_CONFIG', None)
            else:
                os.environ['TECHMC_REMOTE_CONFIG'] = old
        temporary.replace(path)
        print('Configured remote server on local port', args.port)
        return
    remote = for_port(args.port)
    if not remote:
        parser.error('No remote profile for this port; run configure first')
    if args.action == 'install':
        return install_runtime(remote)
    if args.action == 'deploy':
        return deploy(remote)
    if args.action == 'check':
        result = remote.verify_deployment()
        print(json.dumps(result, indent=2))
        if result['status'] != 'matched':
            raise SystemExit(1)
        return
    if args.action == 'status':
        print(json.dumps(remote.state(fresh=True), indent=2))
        return
    if args.action == 'logs':
        print(
            remote.execute(
                "control=root/'runtime/servers'/str(cfg['remote_port'])\n"
                "p=control/'current.json'\n"
                "state=json.loads(p.read_text()) if p.exists() else {}\n"
                "for p in (control/'launcher.log',pathlib.Path(state['directory'])/'supervisor.log' if state else control/'unused'):\n"
                "    if p.exists(): print(p.name+'\\n'+p.read_text(errors='replace')[-12000:])\n"
            ).decode()
        )
        return
    if args.action == 'tunnel':
        remote.fetch_token()
        command = remote.tunnel()
        if not args.background:
            print('Tunnel active while this process runs: local bridge', args.port, flush=True)
            return subprocess.run(command, check=True)
        directory = remote.token_file.parent
        with (directory / 'tunnel.log').open('ab') as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt',
            )
        for _ in range(30):
            if process.poll() is not None:
                raise RuntimeError(
                    'SSH tunnel exited; inspect ' + str(directory / 'tunnel.log')
                )
            import socket

            try:
                with socket.create_connection(('127.0.0.1', args.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            process.terminate()
            raise TimeoutError('SSH tunnel did not start')
        (directory / 'tunnel.json').write_text(
            json.dumps({'pid': process.pid, 'port': args.port}), encoding='utf-8'
        )
        print(
            json.dumps(
                {
                    'status': 'tunnel_started',
                    'pid': process.pid,
                    'bridge_port': args.port,
                    'game_address': '127.0.0.1:' + str(remote.settings['local_game_port']),
                }
            )
        )
        return
    arguments = []
    if args.action in ('run', 'resume'):
        arguments = [
            '--heap-mb',
            str(args.heap_mb),
            '--processors',
            str(args.processors),
            '--game-port',
            str(remote.settings['remote_game_port']),
            '--world-type',
            args.world_type,
            '--seed',
            args.seed,
        ]
        selection = selection_from_args(args)
        if selection is not None:
            selection_path = (
                remote.settings['remote_root']
                + '/runtime/remote-task-selection-'
                + str(remote.settings['remote_port'])
                + '.json'
            )
            remote.execute(
                "pathlib.Path(cfg['path']).write_text(json.dumps(cfg['selection']))\n",
                {'path': selection_path, 'selection': selection},
            )
            arguments += ['--task-selection-file', selection_path]
    print(remote.control(args.action, arguments), end='')


if __name__ == '__main__':
    main()
