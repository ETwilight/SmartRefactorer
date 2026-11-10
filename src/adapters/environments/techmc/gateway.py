"""TechMC 与实验宿主之间的环境端口。

EnvironmentGateway 提供请求、能力发现、反馈投影和证据归档；TechMCProvider
构造环境并接通资源生命周期，算法通过端口读取统一的任务与执行事实。
"""

import copy
from pathlib import Path
import shutil
import json
from environments.techmc.actions.catalog import READ_SERVICES, request_effect


class AllocationFailure(RuntimeError):
    """A host failure with retained admission, cleanup and accounting evidence."""

    def __init__(self, record):
        self.allocation_record = record
        super().__init__(record['error'])


def allocation_failure_record(host, config, exc, archive_errors):
    chain = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({'type': type(current).__name__, 'message': str(current)})
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    # Experiment.run's finally may replace a startup failure with cleanup failure.
    primary = chain[0]
    if primary['message'].startswith('Experiment cleanup incomplete') and len(chain) > 1:
        primary = chain[1]
    record = {
        'status': 'allocation_failed',
        'error': primary['message'],
        'primary_error': primary,
        'exception_chain': chain,
        'host_record': str(getattr(host, 'record_path', host.folder / 'manager.json')),
        'host_status': host.record.get('status'),
        'port': config['port'],
        'method': config['method'],
        'host_archive_errors': archive_errors,
        'cleanup': {
            k: host.record[k]
            for k in (
                'cleanup_errors',
                'resource_released',
                'save_confirmed',
                'remote_finalize_error',
            )
            if k in host.record
        },
    }
    record['checkpoint_host_record'] = str(host.folder / 'manager.json')
    record['capacity_declined'] = host.record.get('capacity_declined') is True
    record['launch_state'] = host.record.get('launch_state')
    record['server_started'] = host.record.get('server_started')
    if host.record.get('start_error'):
        record['host_start_error'] = host.record['start_error']
    launch = Path(record['host_record']).parent / 'launch-error.json'
    try:
        if launch.is_file():
            record['host_launch_error'] = json.loads(launch.read_text(encoding='utf-8'))
            launch_primary = record['host_launch_error'].get('primary_error')
            if (
                isinstance(launch_primary, dict)
                and isinstance(launch_primary.get('type'), str)
                and isinstance(launch_primary.get('message'), str)
            ):
                record['host_error'] = primary
                record['primary_error'] = launch_primary
                record['error'] = launch_primary['message']
    except (OSError, ValueError) as error:
        record['launch_error_read_error'] = {
            'type': type(error).__name__,
            'message': str(error),
        }
    run = Path(config['run_dir'])
    evidence = any(
        (run / path).exists()
        for path in (
            'report.json',
            'log/events.jsonl',
            'events/events.jsonl',
            'accounting/summary.json',
        )
    )
    # No inference from a missing report: a child may have made a paid request
    # before crashing. Only the trusted host's explicit child=None proves no start.
    unstarted = (
        hasattr(host, 'child')
        and host.child is None
        and 'runner_exit_code' not in host.record
        and not evidence
    )
    record['method_started'] = False if unstarted else None
    if unstarted:
        record['method_accounting'] = {
            'state': 'not_started',
            'charged_tokens': 0,
            'rounds': 0,
            'model_calls': 0,
            'proof': {
                'host_record': record['host_record'],
                'host_child_is_none': True,
                'runner_exit_code_absent': True,
                'method_evidence_absent': True,
            },
        }
    return record


# These are public read services, not primitive actions. In particular a registry
# query may need a game connection even though it never mutates game state.


class EnvironmentGateway:
    def __init__(self, environment):
        self.environment = environment

    def __getattr__(self, name):
        return getattr(self.environment, name)

    @property
    def continuation(self):
        value = copy.deepcopy(getattr(self.environment, 'continuation', None))
        approval = self.__dict__.get('_verified_recovery_approval')
        if isinstance(value, dict) and approval is not None:
            value['recovery_approval'] = copy.deepcopy(approval)
        return value

    def bind_recovery_approval(self, approval):
        # Private trusted-host data, not an agent RPC or public task prompt.
        self._verified_recovery_approval = copy.deepcopy(approval)

    @property
    def source_root(self):
        from environments.techmc.contracts import ROOT

        return ROOT

    def source_files(self):
        root = self.source_root
        from environments.techmc.paths import SOURCE_ROOT

        assets = [
            p
            for pattern in (
                '*.py',
                'adapters/*.py',
                'schemas/*.json',
                'tasks/*.json',
                'knowledge/procedures/*/SKILL.md',
            )
            for p in root.glob(pattern)
            if p.is_file()
        ]
        code = [
            p
            for pattern in ('*.py', 'adapters/*.py', 'acceptance/*.py', 'actions/*.py')
            for p in SOURCE_ROOT.glob(pattern)
            if p.is_file()
        ]
        return list(dict.fromkeys(assets + code))

    @property
    def contracts(self):
        from environments.techmc import contracts

        return contracts

    @property
    def failure_contract(self):
        from environments.techmc import failure_contract

        return failure_contract

    @property
    def task_free_methods(self):
        from environments.techmc.actions.catalog import KNOWLEDGE_METHODS
        from environments.techmc.environment import LOCAL_DIAGNOSTICS

        return KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS

    def project_observations(self, observations):
        """MC block names are projected here, never in a method's learning core."""
        import json, re

        blocks = {}
        for observation in observations:
            action = observation['request'].get('params', {}).get('action', {})
            if action.get('method') != 'observe.blocks':
                continue
            result = observation['feedback'].get('result') or {}
            if not isinstance(result, dict):
                continue
            result = result.get('result', result)
            for block in result.get('blocks', []):
                matched = re.fullmatch(
                    r'Block\{minecraft:([a-z0-9_]+)\}', block.get('state', '')
                )
                if block.get('loaded') and matched:
                    blocks[json.dumps(block.get('position'), sort_keys=True)] = matched[1]
        return {'nearby_blocks': list(blocks.values())}

    def handle(self, request):
        return self.environment.handle(request)

    def close(self):
        return self.environment.close()

    def task_index(self, feedback):
        return task_index(feedback)

    def discovery_params(self, op):
        """Legacy bootstrap retained for consumers whose migration is deferred."""
        return (
            {'detail': 'summary'}
            if op == 'environment.describe'
            else {'detail': 'index'} if op == 'actions.list' else {}
        )

    def compact_discovery_params(self, op):
        return (
            {'detail': 'manifest'}
            if op == 'environment.describe'
            else {'detail': 'index'} if op in ('actions.list', 'tasks.list') else {}
        )

    def prefer_default_response(self, request):
        """Region already supplies a bounded view with exact section/read refs."""
        return (
            request.get('op') == 'action.execute'
            and request.get('params', {}).get('action', {}).get('method') == 'observe.region'
        )

    def project_discovery(self, discovery):
        return {
            k: self.task_index(v) if k == 'tasks.list' else copy.deepcopy(v)
            for k, v in discovery.items()
        }

    def archive_evidence(self, folder, reports):
        return archive_environment(folder, self.environment, reports)

    def tool_metadata(self, method):
        if method in READ_SERVICES:
            requires_game = method == 'registry.get'
            return {
                'method': method,
                'category': 'service',
                'effect': 'read_only',
                'requires_game': requires_game,
                'requires_task': False,
                'available_during_evaluation': not requires_game,
                'params_schema': copy.deepcopy(self.contracts.OP_PARAMS[method]),
                'execution_backends': ['direct'],
            }
        from environments.techmc.environment import action_catalog

        return copy.deepcopy(next(t for t in action_catalog() if t['method'] == method))

    def knowledge_methods(self):
        from environments.techmc.actions.catalog import KNOWLEDGE_METHODS

        return sorted(KNOWLEDGE_METHODS | READ_SERVICES)

    def knowledge_request(self, method, params, request_id):
        if method not in self.knowledge_methods():
            raise ValueError('Unknown knowledge method')
        if method in READ_SERVICES:
            return {
                'protocol_version': '1.0',
                'request_id': request_id,
                'op': method,
                'params': copy.deepcopy(params),
            }
        return {
            'protocol_version': '1.0',
            'request_id': request_id,
            'op': 'action.execute',
            'params': {
                'action': {
                    'method': method,
                    'side': 'local',
                    'params': params,
                    'executor': 'direct',
                }
            },
        }

    def readonly(self, request):
        return request_effect(request) == 'read_only'


class TechMCProvider:
    def wrap(self, environment):
        return as_gateway(environment)

    def validate_resume_source(self, source, *, method, recovery_receipt):
        from environments.techmc.explicit_resume import resume_descriptor
        from environments.techmc.contracts import ROOT

        resume = resume_descriptor(
            Path(source).resolve(), root=ROOT, method=method, recovery_receipt=recovery_receipt
        )
        approval = resume.get('recovery_approval')
        if (
            not isinstance(approval, dict)
            or approval.get('verified') is not True
            or approval.get('schema') != 'workflow.resume_recovery_approval.v1'
        ):
            raise ValueError('Saved-source recovery has not been authorized by the environment')
        return copy.deepcopy(approval)

    def recovery_approval(self, config):
        resume = self._resume(config)
        return copy.deepcopy(resume.get('recovery_approval')) if resume else None

    def _resume(self, config):
        options = config.get('options', {})
        source = options.get(config['method'] + '_resume_from')
        if source is None:
            return None
        if config['method'] not in ('baseline', 'psn'):
            raise ValueError('Explicit MC resume only supports Baseline/PSN')
        from environments.techmc.explicit_resume import resume_descriptor
        from environments.techmc.contracts import ROOT

        key = str(Path(source).resolve())
        cached = getattr(self, '_verified_resume', None)
        if cached is None or cached['source_run'] != key:
            cached = resume_descriptor(
                source,
                root=ROOT,
                method=config['method'],
                recovery_receipt=options.get(config['method'] + '_resume_recovery_receipt'),
            )
            self._verified_resume = cached
        if config['port'] != cached['port']:
            raise ValueError('Resume port differs from original saved host')
        return copy.deepcopy(cached)

    def connection_options(self, config):
        options = copy.deepcopy(config.get('environment_options', {}))
        resume = self._resume(config)
        if resume:
            options.update(
                run_id=resume['session_id'],
                actor=resume['actor'],
                server_port=resume['port'],
                task_selection=resume['task_selection'],
                resume_existing=True,
            )
        return options

    def preflight(self, config):
        from environments.techmc.knowledge_backend import preflight

        preflight()
        if config['method'] not in ('baseline', 'psn'):
            return
        if config.get('task_worlds') is False:
            raise ValueError('Baseline and PSN require TaskStream with isolated task worlds')
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
        if config['method'] not in ('baseline', 'psn'):
            return
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

    def run_allocated(self, config, command, cwd):
        from environments.techmc.experiment_manager import Experiment

        options = config.get('options', {})
        selection = (
            {'task_ids': options['task_ids']}
            if options.get('task_ids') is not None
            else config.get('task_selection')
        )
        resume = self._resume(config)
        if resume:
            selection = resume['task_selection']
        host = Experiment(
            port=config['port'],
            heap_mb=config.get('heap_mb', 2048),
            world_type=config.get('world_type', 'void'),
            task_selection=selection,
            task_worlds=config.get('task_worlds', config['method'] in ('baseline', 'psn')),
            experiment_id=resume['experiment_id'] if resume else None,
            resume_saved=resume,
        )
        archive_errors = []
        failure = None
        try:
            code = host.run(command, cwd=cwd)
        except Exception as exc:
            failure = exc
        finally:
            destination = Path(config['run_dir']) / 'environment' / 'host'
            sources = [
                host.folder / 'manager.json',
                getattr(host, 'record_path', host.folder / 'manager.json'),
                host.folder / 'launch-error.json',
                host.folder / 'launcher.log',
            ]
            sources.extend((host.folder / 'resume-history').glob('*.json'))
            try:
                if getattr(host, 'task_worlds', None) is not None:
                    control = host.folder / 'task-world-control'
                    sources.extend(
                        [
                            control / 'worlds.json',
                            *control.glob('requests/*.json'),
                            *control.glob('receipts/*.json'),
                        ]
                    )
            except OSError as exc:
                archive_errors.append(
                    {'source': str(host.folder / 'task-world-control'), 'error': str(exc)}
                )
            for source in sources:
                try:
                    if not source.is_file():
                        continue
                    target = destination / source.relative_to(host.folder)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                except OSError as exc:
                    archive_errors.append({'source': str(source), 'error': str(exc)})
            if archive_errors:
                # Original host records are retained even if their projection is locked.
                host.record['host_archive_errors'] = archive_errors
        if failure is not None:
            raise AllocationFailure(
                allocation_failure_record(host, config, failure, archive_errors)
            ) from failure
        return {
            'status': host.record['status'],
            'host_record': str(getattr(host, 'record_path', host.folder / 'manager.json')),
            'port': config['port'],
            'method': config['method'],
            'worker_exit_code': code,
            'host_archive_errors': archive_errors,
        }

    def connect(self, configuration):
        from environments.techmc import Environment

        return EnvironmentGateway(Environment(**dict(configuration)))

    def auditor(self, folder, environment):
        from audit.projection import ProgressAuditor
        from environments.techmc.adapters.bridge import Bridge
        from urllib.parse import urlsplit

        original = environment.session.server
        bridge = Bridge(
            'audit-' + folder.name,
            environment.actor,
            'server',
            port=urlsplit(original.url).port,
        )
        bridge.token_file = original.token_file
        return ProgressAuditor(folder, server=bridge, schematics=False)

    def slots(self, ports=None):
        from environments.techmc.remote_host import for_port
        from environments.techmc.contracts import ROOT
        import json

        config = ROOT / 'runtime/remote-servers.json'
        remote_ports = (
            list(map(int, json.loads(config.read_text(encoding='utf-8')).get('servers', {})))
            if config.is_file()
            else []
        )
        result = []
        for port in ports or (remote_ports + [8777, 8778, 8779, 8780]):
            remote = for_port(port)
            group = (
                ('remote:' + remote.settings['ssh_host'] + ':' + remote.settings['remote_root'])
                if remote
                else 'local:' + str(port)
            )
            result.append({'port': port, 'group': group, 'remote': remote is not None})
        return result

    def probe(self, slot, heap_mb=2048, controller_mb=512, reserved_mb=0):
        from environments.techmc.contracts import ROOT
        from environments.techmc.remote_host import for_port
        from environments.techmc.experiment_registry import check_existing, host_policy
        from environments.techmc.host_resources import admission
        import json

        remote = for_port(slot['port'])
        try:
            admission(
                ROOT,
                controller_mb=controller_mb + reserved_mb + (0 if remote else heap_mb + 640),
            )
            if remote:
                raw = remote.execute(
                    "sys.path.insert(0,str(root))\nfrom experiment_registry import check_existing,host_policy\n"
                    "from host_resources import admission\n"
                    "try:\n    check_existing(root,cfg['remote_port'],max_servers=host_policy(root))\n"
                    "    admission(root,heap_mb=cfg['heap_mb'])\n    print(json.dumps({'available':True}))\n"
                    "except Exception as exc: print(json.dumps({'available':False,'reason':str(exc)}))\n",
                    {'heap_mb': heap_mb},
                    timeout=20,
                )
                state = json.loads(raw)
                if state.get('available'):
                    deployment = remote.verify_deployment()
                    if deployment.get('status') != 'matched':
                        return {
                            'available': False,
                            'reason': 'Remote deployment differs from this environment; synchronize the idle host before allocation',
                            'requires_action': 'deployment_sync',
                            'deployment': deployment,
                        }
                return state
            check_existing(ROOT, slot['port'], max_servers=host_policy(ROOT))
            admission(ROOT, heap_mb=heap_mb)
            return {'available': True}
        except Exception as exc:
            return {'available': False, 'reason': str(exc), 'type': type(exc).__name__}


def validate_report(name, value):
    from environments.techmc.contracts import validate

    return validate(name, value)


def as_gateway(environment):
    """Wrap a concrete TechMC environment in the provider port when necessary."""
    return (
        environment
        if hasattr(environment, 'contracts') and hasattr(environment, 'tool_metadata')
        else EnvironmentGateway(environment)
    )


def archive_environment(folder, environment, reports):
    """Copy known evidence files into the run; never traverse credentials/runtime."""
    roots = []
    session = getattr(environment, 'session', None)
    if session is not None and hasattr(session, 'directory'):
        roots.append(('session', Path(session.directory)))
    environment_runs = (Path(__file__).resolve().parents[3] / 'assets/techmc/runs').resolve()
    for report in reports:
        source = Path(report['evidence_path']).resolve()
        if source.is_relative_to(environment_runs):
            roots.append((report['evaluation_id'], source))
    artifacts = []
    for label, source in roots:
        if not all(c.isalnum() or c in '-_' for c in label):
            continue
        for name in (
            'api-ledger.json',
            'trace.jsonl',
            'evaluation.json',
            'evidence.json',
            'machines.json',
            'task-progress.json',
            'infrastructure.json',
            'public-documents.json',
            'registries.json',
            'task-stream.json',
            'environment-state.json',
            'task-worlds.json',
        ):
            original = source / name
            if not original.is_file():
                continue
            destination = folder / 'environment' / label / name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
                artifacts.append(
                    {'source': str(original), 'file': str(destination.relative_to(folder))}
                )
            except OSError as exc:
                artifacts.append({'source': str(original), 'error': str(exc)})
        for original in (source / 'world-checkpoints').glob('*.zip'):
            destination = folder / 'environment' / label / 'world-checkpoints' / original.name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
                artifacts.append(
                    {
                        'source': str(original),
                        'file': str(destination.relative_to(folder)),
                        'kind': 'saved_world_checkpoint',
                    }
                )
            except OSError as exc:
                artifacts.append({'source': str(original), 'error': str(exc)})
    return artifacts


def task_index(feedback):
    from environments.techmc.guidance import task_index as project

    return project(feedback)
