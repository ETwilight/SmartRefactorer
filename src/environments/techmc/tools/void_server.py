"""Port-addressed dedicated servers: run creates fresh worlds; resume retains identity."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import argparse
import hashlib
import json
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import os
import signal
import secrets

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.techmc.adapters.bridge import Bridge, BridgeError
from environments.techmc.task_selection import (
    TaskSelection,
    add_selection_arguments,
    selection_from_args,
)
from environments.techmc.tools.bootstrap import ROOT, write_json
from environments.techmc.host_resources import admission
from environments.techmc.host_monitor import Monitor, diagnostics
from environments.techmc.experiment_registry import (
    HostLease,
    check_existing,
    credentials,
    directory as experiment_directory,
    write as write_experiment,
    lease_expired,
    authenticate,
    host_policy,
    allocations,
    retained_instance,
    retained_resume_target,
)

DEFAULT_PORT = 8776
SPAWN = [-40, 64, -24]
SHUTDOWN_TIMEOUT_SECONDS = 90


def check_port(value):
    value = int(value)
    if not 1024 <= value <= 65535:
        raise ValueError('Ports must be 1024..65535')
    return value


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def free_port(port):
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


def control_directory(port):
    return ROOT / 'runtime/servers' / str(check_port(port))


def state(port):
    path = control_directory(port) / 'current.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def physical_copy(source, destination):
    """Copy bytes. Never create aliases, symbolic links, junctions or hard links."""
    source = Path(source)
    destination = Path(destination)
    paths = [source] + list(source.rglob('*')) if source.is_dir() else [source]
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in paths):
        raise ValueError('Linked input assets are not permitted: ' + str(source))
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def prepare_instance(port, game_port, seed, task_selection=None, world_type='void'):
    port = check_port(port)
    game_port = check_port(game_port)
    selection = TaskSelection(task_selection, world_type=world_type)
    if not selection.tasks():
        raise ValueError('No selected tasks are compatible with the requested world type')
    if port == game_port:
        raise ValueError('API and Minecraft game ports must differ')
    template = ROOT / 'runtime/void-research-server'
    instance_id = str(uuid.uuid4())
    directory = control_directory(port) / 'instances' / instance_id
    directory.mkdir(parents=True, exist_ok=False)
    if not directory.resolve().is_relative_to((ROOT / 'runtime/servers').resolve()):
        raise ValueError('Instance escaped runtime')
    receipt = (
        json.loads((ROOT / 'runtime/void-research-server.json').read_text(encoding='utf-8'))
        if world_type == 'void'
        else None
    )
    pack = ROOT / 'runtime/datapacks' / receipt['pack'] if receipt else None
    if pack and hashlib.sha256(pack.read_bytes()).hexdigest() != receipt['pack_sha256']:
        raise ValueError('Empty-world datapack hash mismatch')
    # Selective asset copies: no world, region, entities, player data or links from previous servers.
    for name in ('fabric-server-launch.jar', 'libraries', 'versions'):
        physical_copy(template / name, directory / name)
    cache = template / '.fabric/server'
    if cache.exists():
        physical_copy(cache, directory / '.fabric/server')
    (directory / 'mods').mkdir()
    lock = json.loads((ROOT / 'runtime/mods.candidate-lock.json').read_text(encoding='utf-8'))
    for mod in lock['mods']:
        if mod['project'] in ('fabric-api', 'carpet'):
            source = Path(mod['file'])
            if not source.is_absolute():
                source = ROOT / source
            physical_copy(source, directory / 'mods' / source.name)
    physical_copy(
        ROOT / 'bridge/build/minecraft-agent-26.1.jar',
        directory / 'mods/minecraft-agent-26.1.jar',
    )
    eula = (template / 'eula.txt').read_text(encoding='utf-8')
    if 'eula=true' not in eula.lower():
        raise ValueError('Existing Minecraft EULA acceptance is required')
    (directory / 'eula.txt').write_text(eula, encoding='utf-8')
    for name in ('ops.json', 'whitelist.json', 'banned-players.json', 'banned-ips.json'):
        if (template / name).exists():
            physical_copy(template / name, directory / name)
    world = directory / 'world'
    (world / 'datapacks').mkdir(parents=True)
    if pack:
        physical_copy(pack, world / 'datapacks' / pack.name)
    (world / 'carpet.conf').write_text('hopperCounters true\n', encoding='utf-8')
    properties = {
        'server-ip': '127.0.0.1',
        'server-port': str(game_port),
        'online-mode': 'true',
        'enable-rcon': 'false',
        'enable-command-block': 'false',
        'level-name': 'world',
        'level-type': 'minecraft:normal',
        'level-seed': str(seed),
        'generate-structures': 'true',
        'gamemode': 'creative',
        'difficulty': 'normal',
        'allow-flight': 'true',
        'spawn-protection': '0',
        'view-distance': '6',
        'simulation-distance': '6',
        'max-players': '4',
        'pause-when-empty-seconds': '-1',
        'motd': 'TechMC Environment - API ' + str(port),
        'initial-enabled-packs': 'vanilla,fabric-convention-tags-v2'
        + (',file/' + pack.name if pack else ''),
    }
    (directory / 'server.properties').write_text(
        ''.join(k + '=' + v + '\n' for k, v in properties.items()), encoding='utf-8'
    )
    value = {
        'instance_id': instance_id,
        'status': 'prepared',
        'bridge_port': port,
        'game_port': game_port,
        'game_address': '127.0.0.1:' + str(game_port),
        'directory': str(directory.resolve()),
        'world': str(world.resolve()),
        'seed': str(seed),
        'spawn': SPAWN if world_type == 'void' else None,
        'fresh_world': True,
        'world_type': world_type,
        'created_ns': time.time_ns(),
        'datapack_sha256': receipt['pack_sha256'] if receipt else None,
        'task_selection': selection.spec,
    }
    scenarios = [t for t in selection.tasks() if t.get('spawner_conversion')]
    if scenarios:
        if len(selection.tasks()) != 1:
            raise ValueError(
                'Spawner conversion scenarios require an explicit single-task selection'
            )
        value['spawner_scenario'] = {
            'task_id': scenarios[0]['id'],
            'entity': scenarios[0]['spawner_conversion']['entity'],
            'dimension': (
                'minecraft:the_nether'
                if scenarios[0]['spawner_conversion']['entity']
                in {'minecraft:blaze', 'minecraft:magma_cube'}
                else 'minecraft:overworld'
            ),
            'position': [-40, 64, -20],
            'count': 1,
            'source': 'environment_fixture',
        }
    write_json(directory / 'instance.json', value)
    return value


def bootstrap_platform(process, port, scenario=None, owner_token_file=None):
    def console(text):
        process.stdin.write(text + '\n')
        process.stdin.flush()

    console('setworldspawn -40 64 -24')
    console('gamerule minecraft:respawn_radius 0')
    console('forceload add -42 -26 -38 -22')
    bridge = Bridge(
        'bootstrap-' + str(uuid.uuid4()),
        'ResearchAgent',
        'server',
        port=port,
        timeout=5,
        token_file=owner_token_file,
    )
    deadline = time.monotonic() + 90
    region = {'dimension': 'minecraft:overworld', 'min': [-42, 63, -26], 'max': [-38, 63, -22]}
    loaded = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Server exited during platform initialization')
        try:
            sample = bridge.call('observe.blocks', region)['result']
            if all(b['loaded'] for b in sample['blocks']):
                loaded = True
                break
        except (OSError, BridgeError):
            pass
        time.sleep(0.2)
    if not loaded:
        raise TimeoutError('Initial platform chunk did not load')
    console('fill -42 63 -26 -38 63 -22 minecraft:stone')
    while time.monotonic() < deadline:
        sample = bridge.call('observe.blocks', region)['result']
        if len(sample['blocks']) == 25 and all(
            b['state'] == 'Block{minecraft:stone}' for b in sample['blocks']
        ):
            break
        time.sleep(0.2)
    else:
        raise TimeoutError('Initial stone platform was not verified')
    surround = bridge.call(
        'observe.blocks',
        {'dimension': 'minecraft:overworld', 'min': [-48, 32, -32], 'max': [-33, 95, -17]},
    )['result']
    solid = [
        b
        for b in surround['blocks']
        if b['loaded']
        and b['state']
        not in (
            'Block{minecraft:air}',
            'Block{minecraft:cave_air}',
            'Block{minecraft:void_air}',
        )
    ]
    if len(solid) != 25:
        raise RuntimeError('Fresh platform region contains unexpected blocks')
    if scenario:
        x, y, z = scenario['position']
        entity = scenario['entity']
        if scenario['count'] != 1 or entity not in {
            'minecraft:zombie',
            'minecraft:skeleton',
            'minecraft:spider',
            'minecraft:cave_spider',
            'minecraft:breeze',
            'minecraft:blaze',
            'minecraft:magma_cube',
        }:
            raise ValueError('Invalid spawner fixture')
        block = (
            'minecraft:trial_spawner' if entity == 'minecraft:breeze' else 'minecraft:spawner'
        )
        dimension = scenario['dimension']
        if entity == 'minecraft:blaze' and dimension != 'minecraft:the_nether':
            raise ValueError('Blaze spawner fixtures require the Nether')
        console(f'execute in {dimension} run forceload add {x} {z}')
        data = (
            '{normal_config:{spawn_potentials:[{data:{entity:{id:"minecraft:breeze"}},weight:1}]}}'
            if entity == 'minecraft:breeze'
            else '{SpawnData:{entity:{id:"' + entity + '"}}}'
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            sample = bridge.call(
                'observe.blocks', {'min': [x, y, z], 'max': [x, y, z], 'dimension': dimension}
            )['result']
            if sample['blocks'][0]['loaded']:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Scenario spawner chunk did not load')
        console(f'execute in {dimension} run setblock {x} {y} {z} ' + block + data)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            sample = bridge.call(
                'observe.block_entity', {'x': x, 'y': y, 'z': z, 'dimension': dimension}
            )['result']
            if entity in sample.get('nbt', ''):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Scenario spawner fixture was not verified')
        console(f'execute in {dimension} run forceload remove {x} {z}')
    console('forceload remove -42 -26 -38 -22')
    console('save-all flush')
    return {
        'stone_blocks': 25,
        'platform_min': region['min'],
        'platform_max': region['max'],
        'sampled_blocks': len(surround['blocks']),
        'unexpected_non_air': 0,
        'verified': True,
    }


def validated_resume_candidate(args):
    """Authenticate an explicit retained world; slot admission is separate."""
    explicit = getattr(args, 'instance_id', None)
    current = state(args.port) if not explicit else None
    if not explicit and (not current or current.get('status') != 'stopped'):
        raise ValueError('Resume without an instance ID requires a stopped managed instance')
    experiment_id = (
        getattr(args, 'experiment_id', None)
        or os.environ.get('MC_EXPERIMENT_ID')
        or (current or {}).get('experiment_id')
    )
    if not experiment_id:
        raise PermissionError('Resume requires an existing experiment owner')
    token = (
        (experiment_directory(ROOT, experiment_id) / 'owner.token')
        .read_text(encoding='utf-8')
        .strip()
    )
    if not explicit:
        authenticate(current, token)
        if (
            current.get('experiment_id') != experiment_id
            or current.get('owner_hash') != hashlib.sha256(token.encode()).hexdigest()
        ):
            raise PermissionError('Resume cannot reassign another experiment world')
    return retained_resume_target(
        ROOT, args.port, explicit or current['instance_id'], experiment_id, token
    )


def run_server(args):
    try:
        heap = getattr(args, 'heap_mb', 2048)
        if not 1024 <= heap <= 32768:
            raise ValueError('heap-mb must be 1024..32768')
        check_port(args.port)
        with HostLease(ROOT, port=args.port):
            reserved = False
            reservation = ROOT / 'runtime/reservations' / (str(args.port) + '.json')
            try:
                with HostLease(ROOT, timeout=5):
                    check_existing(
                        ROOT,
                        args.port,
                        getattr(args, 'resume', False),
                        max_servers=host_policy(ROOT),
                        experiment_id=getattr(args, 'experiment_id', None)
                        or os.environ.get('MC_EXPERIMENT_ID'),
                    )
                    budget = admission(ROOT, heap)
                    occupied = {v.get('game_port') for v in allocations(ROOT)}
                    game_port = getattr(args, 'game_port', None)
                    if getattr(args, 'resume', False):
                        candidate = validated_resume_candidate(args)
                        args.instance_id = candidate['instance_id']
                        args.experiment_id = candidate['experiment_id']
                        game_port = candidate.get('game_port', game_port)
                    if game_port is None:
                        game_port = next(
                            (
                                p
                                for p in range(25566, 25666)
                                if p != args.port and p not in occupied and free_port(p)
                            ),
                            None,
                        )
                    if (
                        game_port is None
                        or game_port in occupied
                        or game_port == args.port
                        or not free_port(game_port)
                    ):
                        raise RuntimeError('Minecraft game port is unavailable or reserved')
                    args.game_port = game_port
                    write_experiment(
                        reservation,
                        {
                            'port': args.port,
                            'supervisor_pid': os.getpid(),
                            'created_ns': time.time_ns(),
                            'game_port': game_port,
                            'memory_bytes': (heap + 384) * 1024**2,
                            'disk_bytes': max(2048, heap + 1024) * 1024**2,
                        },
                    )
                    reserved = True
                return _run_server(args, budget)
            finally:
                pending_error = sys.exc_info()[1]
                try:
                    current = state(args.port) or {}
                    if (
                        reserved
                        and not (control_directory(args.port) / 'supervisor.lock').exists()
                        and current.get('status')
                        not in ('starting', 'ready', 'stopping', 'cleanup_failed')
                    ):
                        with HostLease(ROOT, timeout=5):
                            reservation.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    if pending_error is None:
                        raise
                    pending_error.cleanup_errors = [
                        *getattr(pending_error, 'cleanup_errors', []),
                        {
                            'stage': 'reservation.release',
                            'type': type(cleanup_error).__name__,
                            'message': str(cleanup_error),
                        },
                    ]
    except BaseException as exc:
        if getattr(args, 'experiment_id', None):
            import traceback

            try:
                write_experiment(
                    experiment_directory(ROOT, args.experiment_id) / 'launch-error.json',
                    {
                        'type': type(exc).__name__,
                        'message': str(exc),
                        'traceback': traceback.format_exc(),
                        'primary_error': getattr(
                            exc,
                            'primary_error',
                            {'type': type(exc).__name__, 'message': str(exc)},
                        ),
                        'cleanup_errors': getattr(exc, 'cleanup_errors', []),
                    },
                )
            except Exception as publish_error:
                exc.cleanup_errors = [
                    *getattr(exc, 'cleanup_errors', []),
                    {
                        'stage': 'launch_error.publish',
                        'type': type(publish_error).__name__,
                        'message': str(publish_error),
                    },
                ]
        raise


def _run_server(args, budget):
    resumed_candidate = (
        validated_resume_candidate(args) if getattr(args, 'resume', False) else None
    )
    port = check_port(args.port)
    control = control_directory(port)
    control.mkdir(parents=True, exist_ok=True)
    if listening(port):
        raise RuntimeError('API port is already in use: ' + str(port))
    game_port = args.game_port
    if game_port is None:
        game_port = next((p for p in range(25566, 25666) if p != port and free_port(p)), None)
    if game_port is None or not free_port(game_port):
        raise RuntimeError('Minecraft game port is unavailable')
    mutex = control / 'supervisor.lock'
    with mutex.open('x', encoding='utf-8') as stream:
        stream.write(str(time.time_ns()))
    process = None
    value = None
    reader = None
    monitor = None
    health_stop = threading.Event()
    experiment = None
    previous = state(port) if getattr(args, 'resume', False) else None
    experiment_id = (
        getattr(args, 'experiment_id', None)
        or os.environ.get('MC_EXPERIMENT_ID')
        or (previous or {}).get('experiment_id')
        or str(uuid.uuid4())
    )
    owner_path, owner_hash = credentials(ROOT, experiment_id)
    experiment_path = experiment_directory(ROOT, experiment_id)
    write_experiment(experiment_path / 'heartbeat.json', {'wall_ns': time.time_ns()})

    def interrupted(signum, frame):
        raise KeyboardInterrupt('Supervisor received signal ' + str(signum))

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, interrupted)
    try:
        if getattr(args, 'resume', False):
            value = resumed_candidate
            directory = Path(value['directory']).resolve()
            if not directory.is_relative_to(
                (ROOT / 'runtime/servers' / str(port) / 'instances').resolve()
            ):
                raise ValueError('Invalid resume directory')
            game_port = value['game_port']
            if not free_port(game_port):
                raise RuntimeError('Original game port is occupied')
            physical_copy(
                ROOT / 'bridge/build/minecraft-agent-26.1.jar',
                directory / 'mods/minecraft-agent-26.1.jar',
            )
            value.update(
                resumed_existing_world=True, resumed_ns=time.time_ns(), fresh_world=False
            )
            if 'exit_code' in value:
                value.setdefault('previous_exits', []).append(
                    {k: value.get(k) for k in ('exit_code', 'stopped_ns', 'supervisor_error')}
                )
                for key in ('exit_code', 'stopped_ns', 'supervisor_error'):
                    value.pop(key, None)
            requested_selection = selection_from_args(args)
            if requested_selection is not None:
                value['previous_task_selection'] = value['task_selection']
                value['task_selection'] = TaskSelection(
                    requested_selection, world_type=value.get('world_type', 'void')
                ).spec
        else:
            value = prepare_instance(
                port, game_port, args.seed, selection_from_args(args), args.world_type
            )
        directory = Path(value['directory'])
        value.update(
            experiment_id=experiment_id,
            owner_hash=owner_hash,
            owner_token_file=str(owner_path),
            admission=budget,
        )
        launch_id = os.environ.get('MC_EXPERIMENT_LAUNCH_ID')
        if launch_id:
            if str(uuid.UUID(launch_id)) != launch_id:
                raise ValueError('Invalid explicit startup epoch')
            value['launch_id'] = launch_id
        else:
            value.pop('launch_id', None)
        experiment = {
            'experiment_id': experiment_id,
            'owner_hash': owner_hash,
            'status': 'starting',
            'port': port,
            'instance_id': value['instance_id'],
            'world': value['world'],
            'directory': value['directory'],
            'started_ns': time.time_ns(),
            'admission': budget,
            'retention': 'keep_until_explicit_prune',
        }
        if launch_id:
            experiment['launch_id'] = launch_id
        write_experiment(experiment_path / 'experiment.json', experiment)
        for name in ('stop-request.json', 'save-request.json'):
            pending = control / name
            if pending.exists():
                pending.rename(pending.with_name(name + '.stale-' + str(time.time_ns())))
        java = json.loads((ROOT / 'runtime/java.json').read_text(encoding='utf-8'))['java']
        heap = getattr(args, 'heap_mb', 3072)
        if not 1024 <= heap <= 32768:
            raise ValueError('heap-mb must be 1024..32768')
        processors = getattr(args, 'processors', 4)
        if not 1 <= processors <= 256:
            raise ValueError('processors must be 1..256')
        command = [
            java,
            '-Xmx' + str(heap) + 'M',
            '-XX:+HeapDumpOnOutOfMemoryError',
            '-XX:HeapDumpPath=' + str(directory / 'oom.hprof'),
            '-XX:ErrorFile=' + str(directory / 'hs_err_pid%p.log'),
            '-Dminecraft.agent.token_file=' + str(owner_path),
            '-XX:ActiveProcessorCount=' + str(processors),
            '-Xlog:gc*:file=gc.log:time,uptime,level,tags:filecount=3,filesize=10M',
            '-Dminecraft.agent.root=' + str(ROOT),
            '-Dminecraft.agent.server.port=' + str(port),
            '-Dminecraft.agent.instance=' + value['instance_id'],
            '-Dminecraft.agent.natural_world=' + str(value['world_type'] == 'natural').lower(),
            '-jar',
            'fabric-server-launch.jar',
            'nogui',
        ]
        # A child-owned file remains drainable even if the Python supervisor
        # fails. A PIPE would depend on its daemon reader staying alive.
        log_path = directory / 'supervisor.log'
        log_offset = log_path.stat().st_size if log_path.exists() else 0
        # Preserve the displaced slot pointer as evidence; never forge it to
        # look owned before launching the explicitly authenticated target.
        pointer = control / 'current.json'
        if getattr(args, 'resume', False) and pointer.exists():
            previous_raw = pointer.read_bytes()
            previous_pointer = json.loads(previous_raw)
            if previous_pointer.get('instance_id') != value['instance_id']:
                history = control / 'resume-slot-history'
                history.mkdir(parents=True, exist_ok=True)
                archive = history / (hashlib.sha256(previous_raw).hexdigest() + '.json')
                try:
                    with archive.open('xb') as stream:
                        stream.write(previous_raw)
                except FileExistsError:
                    if archive.read_bytes() != previous_raw:
                        raise ValueError('Slot history snapshot differs from its content hash')
        with log_path.open('a', encoding='utf-8') as output:
            process = subprocess.Popen(
                command,
                cwd=directory,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
        monitor = Monitor(process.pid, directory).start()
        value.update(
            status='starting',
            shutdown_save_confirmed=False,
            pid=process.pid,
            heap_mb=heap,
            processors=processors,
            supervisor_pid=__import__('os').getpid(),
        )
        write_json(control / 'current.json', value)
        print(
            json.dumps(
                {
                    'status': 'starting',
                    'bridge_port': port,
                    'game_address': value['game_address'],
                    'instance_id': value['instance_id'],
                    'world': value['world'],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        ready = threading.Event()
        saved = threading.Event()
        console_commands = queue.Queue()
        save_label = None
        health_stop = threading.Event()

        def sample_health():
            health = Bridge(
                'health-' + value['instance_id'],
                'host',
                'server',
                port=port,
                timeout=3,
                token_file=owner_path,
            )
            while not health_stop.wait(5):
                if not ready.is_set():
                    continue
                try:
                    sample = {
                        'wall_ns': time.time_ns(),
                        'result': health.call('observe.health')['result'],
                    }
                    monitor.heap(sample['result'])
                except Exception as exc:
                    sample = {'wall_ns': time.time_ns(), 'error': str(exc)[:1000]}
                with (directory / 'health.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(sample) + '\n')

        threading.Thread(target=sample_health, daemon=True).start()

        def read_logs():
            with log_path.open('r', encoding='utf-8', errors='replace') as log:
                log.seek(log_offset)
                while True:
                    line = log.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue
                    if args.console:
                        print(line, end='', flush=True)
                    if 'Done (' in line:
                        ready.set()
                    if 'Saved the game' in line:
                        saved.set()
                    if 'All dimensions are saved' in line:
                        value['last_world_save_log_ns'] = time.time_ns()

        reader = threading.Thread(target=read_logs, daemon=True)
        reader.start()
        if args.console:

            def read_console():
                for line in sys.stdin:
                    console_commands.put(line.strip())

            threading.Thread(target=read_console, daemon=True).start()
        deadline = time.monotonic() + 180
        initialized = False
        stopping = False
        last_periodic = time.monotonic()
        stop_reason = None
        while process.poll() is None:
            if save_label is not None and saved.is_set():
                value['last_confirmed_save_ns'] = time.time_ns()
                write_json(
                    control / 'save-confirmation.json',
                    {
                        'instance_id': value['instance_id'],
                        'label': save_label,
                        'saved_ns': value['last_confirmed_save_ns'],
                    },
                )
                write_json(control / 'current.json', value)
                save_label = None
            if not initialized and not stopping and ready.is_set():
                if not getattr(args, 'resume', False):
                    value['platform'] = (
                        bootstrap_platform(
                            process, port, value.get('spawner_scenario'), owner_path
                        )
                        if value['world_type'] == 'void'
                        else {'world_type': 'natural', 'terrain_modified': False}
                    )
                initialized = True
                value['status'] = 'ready'
                experiment['status'] = 'running'
                write_experiment(experiment_path / 'experiment.json', experiment)
                write_json(control / 'current.json', value)
                write_json(directory / 'instance.json', value)
                print(
                    json.dumps(
                        {
                            'status': 'ready',
                            'bridge_port': port,
                            'game_address': value['game_address'],
                            'platform': value['platform'],
                            'instance_id': value['instance_id'],
                        }
                    ),
                    flush=True,
                )
            if not initialized and not stopping and time.monotonic() > deadline:
                raise TimeoutError('Server startup timed out')
            if not stopping and lease_expired(
                ROOT, experiment_id, getattr(args, 'lease_seconds', 120)
            ):
                stop_reason = 'owner_heartbeat_expired'
                value['stop_requested_ns'] = time.time_ns()
                process.stdin.write('stop\n')
                process.stdin.flush()
                stopping = True
            if (
                initialized
                and not stopping
                and save_label is None
                and time.monotonic() - last_periodic >= 60
            ):
                saved.clear()
                save_label = 'periodic-' + str(time.time_ns())
                process.stdin.write('save-all flush\n')
                process.stdin.flush()
                last_periodic = time.monotonic()
            try:
                line = console_commands.get_nowait()
            except queue.Empty:
                line = None
            if line and not stopping:
                process.stdin.write(line + '\n')
                process.stdin.flush()
                if line.lstrip('/') == 'stop':
                    stopping = True
                    value['stop_requested_ns'] = time.time_ns()
            for name, command_text in (
                ('save-request.json', 'save-all flush'),
                ('stop-request.json', 'stop'),
            ):
                pending = control / name
                if pending.exists() and not stopping:
                    request = json.loads(pending.read_text(encoding='utf-8'))
                    if request.get('instance_id') != value['instance_id']:
                        raise ValueError('Control request targets another instance')
                    if command_text == 'save-all flush':
                        saved.clear()
                        save_label = request.get('label', 'manual')
                    process.stdin.write(command_text + '\n')
                    process.stdin.flush()
                    pending.unlink()
                    if command_text == 'stop':
                        stopping = True
                        value['stop_requested_ns'] = time.time_ns()
            time.sleep(0.15)
        reader.join(timeout=3)
        health_stop.set()
        value.update(status='stopped', exit_code=process.returncode, stopped_ns=time.time_ns())
        write_json(control / 'current.json', value)
        write_json(directory / 'instance.json', value)
        if process.returncode != 0:
            raise RuntimeError('Server failed; inspect ' + str(directory / 'supervisor.log'))
    except BaseException as exc:
        primary_error = exc
        if value is not None:
            value['supervisor_error'] = {'type': type(exc).__name__, 'message': str(exc)}
            try:
                write_json(
                    Path(value['directory']) / 'supervisor-error.json',
                    value['supervisor_error'],
                )
            except Exception as secondary:
                exc.cleanup_errors = [
                    {
                        'stage': 'supervisor_error.publish',
                        'type': type(secondary).__name__,
                        'message': str(secondary),
                    }
                ]
        raise
    finally:
        primary_error = locals().get('primary_error')
        try:
            if process is not None and process.poll() is None:
                try:
                    if not locals().get('stopping', False):
                        value['stop_requested_ns'] = time.time_ns()
                        process.stdin.write('stop\n')
                        process.stdin.flush()
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                    if reader:
                        reader.join(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as shutdown_error:
                    if primary_error is not None:
                        primary_error.cleanup_errors = [
                            *getattr(primary_error, 'cleanup_errors', []),
                            {
                                'stage': 'shutdown.wait',
                                'type': type(shutdown_error).__name__,
                                'message': str(shutdown_error),
                            },
                        ]
                    value.update(
                        status='cleanup_failed',
                        cleanup_error='Java did not confirm shutdown; host remains unavailable',
                    )
                    write_json(control / 'current.json', value)
                    if experiment:
                        experiment.update(
                            status='cleanup_failed',
                            resource_released=False,
                            save_confirmed=False,
                        )
                        write_experiment(experiment_path / 'experiment.json', experiment)
                    health_stop.set()
                    if monitor:
                        monitor.close()
                    diagnostics(Path(value['directory']), None, value.get('supervisor_error'))
                    failure = RuntimeError(
                        'Server still running; refusing to terminate it without a complete save'
                    )
                    failure.cleanup_errors = [
                        {
                            'stage': 'shutdown.wait',
                            'type': type(shutdown_error).__name__,
                            'message': str(shutdown_error),
                        }
                    ]
                    raise failure from shutdown_error
            if process is not None and value is not None:
                value['stop_reason'] = locals().get('stop_reason')
                value['shutdown_save_confirmed'] = process.returncode == 0 and value.get(
                    'last_world_save_log_ns', 0
                ) >= value.get('stop_requested_ns', 2**63)
                value.update(
                    status='stopped', exit_code=process.returncode, stopped_ns=time.time_ns()
                )
                write_json(control / 'current.json', value)
                write_json(Path(value['directory']) / 'instance.json', value)
            health_stop.set()
            if monitor:
                monitor.close()
            if process is not None and process.poll() is not None:
                if reader:
                    reader.join(timeout=3)
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        pipe.close()
            if value is not None:
                report = diagnostics(
                    Path(value['directory']),
                    process.returncode if process else None,
                    value.get('supervisor_error'),
                )
                if experiment is not None:
                    experiment.update(
                        status=(
                            'abandoned'
                            if locals().get('stop_reason') == 'owner_heartbeat_expired'
                            else (
                                'stopped'
                                if process is not None and process.returncode == 0
                                else 'interrupted'
                            )
                        ),
                        finished_ns=(
                            time.time_ns()
                            if locals().get('stop_reason') == 'owner_heartbeat_expired'
                            else None
                        ),
                        resource_released=process is not None and process.poll() is not None,
                        shutdown_save_confirmed=value.get('shutdown_save_confirmed', False),
                        stopped_ns=time.time_ns(),
                        exit_code=process.returncode if process else None,
                        stop_reason=locals().get('stop_reason'),
                        save_confirmed_ns=value.get('last_confirmed_save_ns'),
                        diagnostics=report,
                    )
                    write_experiment(experiment_path / 'experiment.json', experiment)
            mutex.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            if primary_error is not None and cleanup_error is not primary_error:
                primary_error.primary_error = {
                    'type': type(primary_error).__name__,
                    'message': str(primary_error),
                }
                details = getattr(cleanup_error, 'cleanup_errors', None) or [
                    {
                        'stage': 'supervisor.finalize',
                        'type': type(cleanup_error).__name__,
                        'message': str(cleanup_error),
                    }
                ]
                errors = list(getattr(primary_error, 'cleanup_errors', []))
                for detail in details:
                    if detail not in errors:
                        errors.append(detail)
                primary_error.cleanup_errors = errors
                raise primary_error from cleanup_error
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'resume', 'stop', 'save', 'status'])
    parser.add_argument(
        '--port',
        type=int,
        default=DEFAULT_PORT,
        help='Environment API/bridge port; shared with baseline --port',
    )
    parser.add_argument(
        '--game-port',
        type=int,
        help='Minecraft game port; default is first unused port starting at 25566',
    )
    parser.add_argument('--seed', default='-7160105646644042722')
    parser.add_argument('--world-type', choices=['void', 'natural'], default='void')
    parser.add_argument('--console', action='store_true')
    parser.add_argument('--experiment-id')
    parser.add_argument(
        '--instance-id', help='Explicit retained instance for authenticated resume only'
    )
    parser.add_argument('--lease-seconds', type=int, default=120)
    parser.add_argument('--heap-mb', type=int, default=2048)
    parser.add_argument(
        '--processors',
        type=int,
        default=4,
        help='JVM processor budget; use 1 on a small remote VM',
    )
    add_selection_arguments(parser)
    args = parser.parse_args()
    check_port(args.port)
    if args.instance_id and args.action != 'resume':
        parser.error('--instance-id is valid only for resume')
    if args.action in ('run', 'resume'):
        args.resume = args.action == 'resume'
        return run_server(args)
    value = state(args.port)
    if args.action == 'status':
        if value is None:
            print(
                json.dumps(
                    {
                        'bridge_port': args.port,
                        'status': 'not_started',
                        'api_listening': listening(args.port),
                    }
                )
            )
        else:
            print(
                json.dumps(
                    {
                        **value,
                        'api_listening': listening(args.port),
                        'game_listening': listening(value['game_port']),
                    },
                    indent=2,
                )
            )
        return
    if value is None or value.get('status') not in ('starting', 'ready'):
        raise RuntimeError('No running managed instance on API port ' + str(args.port))
    token_file = os.environ.get('MC_EXPERIMENT_TOKEN_FILE')
    token = Path(token_file).read_text().strip() if token_file else None
    authenticate(value, token)
    expected_experiment = getattr(args, 'experiment_id', None) or os.environ.get(
        'MC_EXPERIMENT_ID'
    )
    if expected_experiment and (
        value.get('experiment_id') != expected_experiment
        or value.get('owner_hash') != hashlib.sha256((token or '').encode()).hexdigest()
    ):
        raise PermissionError('Lifecycle control belongs to a different experiment')
    if args.action == 'save' and not listening(args.port):
        raise RuntimeError('Selected instance is not ready')
    control = control_directory(args.port)
    write_json(
        control / (args.action + '-request.json'),
        {'action': args.action, 'instance_id': value['instance_id']},
    )
    if args.action == 'save':
        print('Save requested on API port ' + str(args.port))
        return
    print('Graceful stop requested on API port ' + str(args.port), flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = state(args.port)
        if (
            current
            and current['instance_id'] == value['instance_id']
            and current.get('status') == 'stopped'
            and not (control / 'supervisor.lock').exists()
        ):
            if current.get('exit_code') != 0 or not current.get(
                'shutdown_save_confirmed', False
            ):
                raise RuntimeError(
                    'Server stopped without a confirmed clean world save; inspect failure-report.json'
                )
            print('Stopped and saved API port ' + str(args.port))
            return
        time.sleep(0.3)
    raise TimeoutError('Selected server has not confirmed a complete shutdown')


if __name__ == '__main__':
    main()
