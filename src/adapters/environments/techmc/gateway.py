"""Connect TechMC environment sessions, host allocations, and evidence views to the shared
runtime.
"""

import copy
from pathlib import Path
import shutil
import json
from environments.techmc.actions.catalog import READ_SERVICES, request_effect


class AllocationFailure(RuntimeError):
    """Carry the host's allocation-failure record to the worker and capacity queue.

    allocation_record is the supplied dictionary containing the primary error,
    exception chain, startup state, cleanup status, and available accounting
    evidence. Its error field initializes the RuntimeError message. The queue
    reads this record when classifying an allocation as waiting for capacity.

    Example, in the host failure handler:
        record = allocation_failure_record(host, config, exc, archive_errors)
        raise AllocationFailure(record) from exc

    The raised error retains the host evidence alongside its original cause.
    """

    def __init__(self, record):
        """Retain the allocation record and use its primary error as the exception message."""

        self.allocation_record = record
        super().__init__(record['error'])


def allocation_failure_record(host, config, exc, archive_errors):
    """Collect startup, cleanup, exception-chain, and method-start evidence into one
    allocation record.
    """
    # chain: list[dict[str, str]], following exception causes or contexts until a repeated
    # object.

    chain = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({'type': type(current).__name__, 'message': str(current)})
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    # A cleanup exception can wrap the earlier startup exception in Experiment.run().
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
    # Check the host's explicit child state together with recorded method evidence.
    # An absent report alone leaves the child's start and usage state unknown.
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


# READ_SERVICES contains public operations alongside the local knowledge methods.
# registry.get uses a game connection and has its own requires_game metadata.


class EnvironmentGateway:
    """Expose a concrete TechMC Environment through the shared environment-port interface.

    environment owns the session, task state, request ledger, and cleanup.
    handle() and close() delegate to it; __getattr__ forwards other environment
    operations. The gateway supplies discovery projections, action metadata,
    knowledge/read-service request construction, source paths, and evidence
    export. contracts and failure_contract expose the TechMC schema and response
    modules. source_root identifies project-owned assets; source_files() also
    includes the separate executable Python package.

    _verified_recovery_approval is an optional copied host approval attached by
    bind_recovery_approval(). continuation returns a copied environment
    descriptor with that approval when available. Public read services and local
    knowledge methods share knowledge_request(); registry.get requires a game
    connection, as indicated by its tool metadata.

    Example:
        gateway = EnvironmentGateway(environment)
        request = gateway.knowledge_request(
            'schemas.get', {'name': 'request'}, request_id
        )
        feedback = gateway.handle(request)

    feedback is the environment response containing the request schema. The
    same gateway gives the method its effect metadata and retained-feedback access.
    """

    def __init__(self, environment):
        """Bind the concrete environment served by this port."""

        self.environment = environment

    def __getattr__(self, name):
        """Forward an attribute not supplied by the gateway to its environment."""

        return getattr(self.environment, name)

    @property
    def continuation(self):
        """Return a copied continuation descriptor with the attached host approval when
        present.
        """

        value = copy.deepcopy(getattr(self.environment, 'continuation', None))
        approval = self.__dict__.get('_verified_recovery_approval')
        if isinstance(value, dict) and approval is not None:
            value['recovery_approval'] = copy.deepcopy(approval)
        return value

    def bind_recovery_approval(self, approval):
        """Retain a copy of the environment-verified approval for host continuation handling."""
        # Store this verified approval for the host's continuation descriptor.

        self._verified_recovery_approval = copy.deepcopy(approval)

    @property
    def source_root(self):
        """Return the Path containing project-owned TechMC source assets."""

        from environments.techmc.paths import ASSET_ROOT

        return ASSET_ROOT

    def source_files(self):
        """List declared task, schema, procedure, and Python source files for run archival."""

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
        """Return the TechMC JSON-schema module."""

        from environments.techmc import contracts

        return contracts

    @property
    def failure_contract(self):
        """Return the TechMC execution and failure-normalization module."""

        from environments.techmc import failure_contract

        return failure_contract

    @property
    def task_free_methods(self):
        """Return knowledge and local diagnostic method names available without an active
        task.
        """

        from environments.techmc.actions.catalog import KNOWLEDGE_METHODS
        from environments.techmc.environment import LOCAL_DIAGNOSTICS

        return KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS

    def project_observations(self, observations):
        """Collect loaded block names from observe.blocks feedback, retaining the latest
        value per position.
        """
        import json, re

        # blocks: dict[str, str], mapping JSON-encoded positions to their latest observed block
        # name.
        blocks = {}
        for observation in observations:
            action = observation['request'].get('params', {}).get('action', {})
            if action.get('method') != 'observe.blocks':
                continue
            result = observation['feedback'].get('result') or {}
            if not isinstance(result, dict):
                continue
            # Select the operation-result dictionary when feedback still contains a native RPC
            # envelope.
            result = result.get('result', result)
            for block in result.get('blocks', []):
                matched = re.fullmatch(
                    r'Block\{minecraft:([a-z0-9_]+)\}', block.get('state', '')
                )
                if block.get('loaded') and matched:
                    blocks[json.dumps(block.get('position'), sort_keys=True)] = matched[1]
        return {'nearby_blocks': list(blocks.values())}

    def handle(self, request):
        """Dispatch a protocol request through the concrete environment."""

        return self.environment.handle(request)

    def close(self):
        """Close the concrete environment and return its cleanup result."""

        return self.environment.close()

    def task_index(self, feedback):
        """Project task-list feedback into the shared task-index view."""

        return task_index(feedback)

    def discovery_params(self, op):
        """Select summary environment discovery and indexed action discovery parameters."""
        return (
            {'detail': 'summary'}
            if op == 'environment.describe'
            else {'detail': 'index'} if op == 'actions.list' else {}
        )

    def compact_discovery_params(self, op):
        """Select manifest environment discovery and indexed action/task discovery
        parameters.
        """

        return (
            {'detail': 'manifest'}
            if op == 'environment.describe'
            else {'detail': 'index'} if op in ('actions.list', 'tasks.list') else {}
        )

    def prefer_default_response(self, request):
        """Select the provider's region-specific default view for observe.region requests."""
        return (
            request.get('op') == 'action.execute'
            and request.get('params', {}).get('action', {}).get('method') == 'observe.region'
        )

    def project_discovery(self, discovery):
        """Copy discovery feedback, projecting the task catalogue through task_index()."""

        return {
            k: self.task_index(v) if k == 'tasks.list' else copy.deepcopy(v)
            for k, v in discovery.items()
        }

    def archive_evidence(self, folder, reports):
        """Copy the session and evaluation evidence into the shared run layout."""

        return archive_environment(folder, self.environment, reports)

    def tool_metadata(self, method):
        """Return service metadata or a copied primitive action contract."""

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
        """List local knowledge methods and public read-service operation names."""

        from environments.techmc.actions.catalog import KNOWLEDGE_METHODS

        return sorted(KNOWLEDGE_METHODS | READ_SERVICES)

    def knowledge_request(self, method, params, request_id):
        """Build a direct service request or local knowledge action.execute request."""

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
        """Classify a request using TechMC's public effect metadata."""

        return request_effect(request) == 'read_only'


class TechMCProvider:
    """Construct TechMC ports and manage their allocated host lifecycle.

    The provider resolves connection options and verifies explicit saved-run
    descriptors. _verified_resume lazily caches a verified Baseline or PSN
    descriptor by source directory; _resume() returns a copy after checking the
    configured port. configure_run() attaches their task stream and retained
    world manager, or restores their verified saved binding.

    run_allocated() owns an Experiment for the child command, collects its exit
    result, and copies host records into the run's environment/host directory.
    AllocationFailure carries failed-start and cleanup evidence. slots() groups
    local and remote ports for scheduling; probe() checks the corresponding
    host capacity and deployment. auditor() creates a separate audit bridge.

    Example:
        provider = TechMCProvider()
        gateway = provider.connect(provider.connection_options(config))
        provider.configure_run(gateway, config)
        gateway.close()

    This constructs and configures the worker's environment port, then closes
    it. The allocation worker calls run_allocated() to manage the server and
    method process around that connection lifecycle.
    """

    def wrap(self, environment):
        """Return the existing environment port or wrap a concrete TechMC environment."""

        return as_gateway(environment)

    def validate_resume_source(self, source, *, method, recovery_receipt):
        """Verify the saved-source recovery receipt and return its copied approval record."""

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
        """Return the approval from the configured verified continuation, or None."""

        resume = self._resume(config)
        return copy.deepcopy(resume.get('recovery_approval')) if resume else None

    def _resume(self, config):
        """Load or reuse the selected Baseline/PSN continuation descriptor and check its
        original port.
        """

        options = config.get('options', {})
        source = options.get(config['method'] + '_resume_from')
        if source is None:
            return None
        if config['method'] not in ('baseline', 'psn'):
            raise ValueError('Explicit MC resume only supports Baseline/PSN')
        from environments.techmc.explicit_resume import resume_descriptor
        from environments.techmc.contracts import ROOT

        key = str(Path(source).resolve())
        # cached: dict | None, containing the previously verified saved-run descriptor.
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
        """Merge configured connection options with the verified saved session identity when
        resuming.
        """

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
        """Check knowledge dependencies and the Baseline/PSN task-stream configuration or
        saved descriptor.
        """

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
        """Configure Baseline/PSN task delivery and worlds, restoring the saved visit when
        requested.
        """

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
        """Run the child command under an Experiment allocation and archive the resulting
        host records.
        """

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
                # Record failed archive copies alongside the host result.
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
        """Construct a concrete Environment from connection options and return its gateway."""

        from environments.techmc import Environment

        return EnvironmentGateway(Environment(**dict(configuration)))

    def auditor(self, folder, environment):
        """Create a round auditor with a separate server bridge and schematic export
        disabled.
        """

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
        """List requested or configured ports with their local/remote scheduling groups."""

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
        """Check controller and server capacity, active allocations, and remote deployment
        readiness.
        """

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
                # raw: str returned by the remote Python command; state is its decoded
                # capacity-result dictionary.
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
    """Validate a report value against a named TechMC schema."""

    from environments.techmc.contracts import validate

    return validate(name, value)


def as_gateway(environment):
    """Return an existing environment port or construct an EnvironmentGateway around it."""
    return (
        environment
        if hasattr(environment, 'contracts') and hasattr(environment, 'tool_metadata')
        else EnvironmentGateway(environment)
    )


def archive_environment(folder, environment, reports):
    """Copy named session/evaluation records and saved-world checkpoints into the run,
    returning artifact records.
    """
    roots = []
    session = getattr(environment, 'session', None)
    if session is not None and hasattr(session, 'directory'):
        roots.append(('session', Path(session.directory)))
    from environments.techmc.paths import DATA_ROOT

    environment_runs = (DATA_ROOT / 'runs').resolve()
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
    """Project task-list feedback through TechMC's guidance task-index implementation."""

    from environments.techmc.guidance import task_index as project

    return project(feedback)
