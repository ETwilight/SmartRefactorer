"""Owner-authorized retirement of one legacy, provably passive wait.

The original unknown response remains unknown. This grants only a new decision
after the preserved execution prefix; it never replays that prefix or the wait.
"""

import ast
import copy
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import time

from .contracts import ROOT
from .experiment_registry import (
    _read_json_retry,
    authenticate,
    retained_instance,
    retained_resume_target,
    process_alive,
)
from .remote_host import for_port
from .task_catalog import select_profile
from .task_selection import TaskSelection

SCHEMA = 'techmc.readonly_wait_recovery.v1'
APPROVAL_SCHEMA = 'workflow.resume_recovery_approval.v1'


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False
        ).encode('utf-8')
    ).hexdigest()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid checkpoint identity')
    return value


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id + '.' + node.attr
    return None


def prove_passive_wait_source(path, expected_sha256):
    """Inspect the archived implementation without importing/executing it."""
    if _sha(path) != expected_sha256:
        raise ValueError('Archived wait source does not match its source manifest')
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    functions = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'natural_wait'
    ]
    if len(functions) != 1:
        raise ValueError('Missing unique archived natural_wait implementation')
    function = functions[0]
    if function.decorator_list:
        raise ValueError('Decorated wait is not a proven passive implementation')
    allowed = {
        'ValueError',
        'RuntimeError',
        'time.monotonic',
        'time.sleep',
        'server.call',
        'check_clock',
        'cancel',
    }
    protected = {
        'server',
        'time',
        'cancel',
        'ValueError',
        'RuntimeError',
        'natural_wait',
        'check_clock',
    }
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node.id in protected
        ):
            raise ValueError('Wait rebinds a trusted read-only call target')
        if (
            isinstance(node, ast.FunctionDef)
            and node is not function
            and (node.name != 'check_clock' or node.decorator_list)
        ):
            raise ValueError('Wait defines an unproved helper')
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Global,
                ast.Nonlocal,
                ast.ClassDef,
                ast.AsyncFunctionDef,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
                ast.With,
                ast.AsyncWith,
            ),
        ):
            raise ValueError('Wait contains an unproved execution mechanism')
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            raise ValueError('Wait writes external state')
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name not in allowed:
                raise ValueError('Wait contains an unproved call')
            if name == 'server.call' and (
                len(node.args) != 1
                or not isinstance(node.args[0], ast.Constant)
                or node.args[0].value != 'clock.query'
                or node.keywords
            ):
                raise ValueError('Wait contains a non-clock-read RPC')
    # The generated request cannot supply a custom cancellation callback.
    arguments = [argument.arg for argument in function.args.args]
    defaults = dict(
        zip(arguments[len(arguments) - len(function.args.defaults) :], function.args.defaults)
    )
    cancel = defaults.get('cancel')
    if (
        not isinstance(cancel, ast.Lambda)
        or cancel.args.args
        or not isinstance(cancel.body, ast.Constant)
        or cancel.body.value is not False
    ):
        raise ValueError('Wait cancellation callback is not the inert default')
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'ActionSession'
    ]
    invokes = [
        node
        for cls in classes
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == 'invoke'
    ]
    branches = [
        node
        for invoke in invokes
        for node in ast.walk(invoke)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == 'method'
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == 'experiment.wait'
    ]
    if len(branches) != 1:
        raise ValueError('Missing unique passive wait dispatch branch')
    calls = [
        node
        for statement in branches[0].body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
    ]
    if any(_call_name(node.func) not in ('natural_wait', 'RuntimeError') for node in calls):
        raise ValueError('Wait dispatch contains another effect')
    natural = [node for node in calls if _call_name(node.func) == 'natural_wait']
    if (
        len(natural) != 1
        or len(natural[0].args) != 1
        or ast.dump(natural[0].args[0])
        != ast.dump(
            ast.Attribute(
                value=ast.Name(id='self', ctx=ast.Load()), attr='server', ctx=ast.Load()
            )
        )
    ):
        raise ValueError('Wait dispatch does not use the owned server read interface')
    return {
        'runtime_support_sha256': expected_sha256,
        'rpc_methods': ['clock.query'],
        'synchronous': True,
        'writes_game_state': False,
    }


def _unresolved(response):
    return response.get('execution', {}).get('state') in ('unknown', 'running') or response.get(
        'status'
    ) in ('pending_or_unknown', 'running')


def _legacy_knowledge_error(entry):
    request = entry.get('request', {})
    response = entry.get('response', {})
    if (
        response.get('status') != 'error'
        or response.get('execution', {}).get('retry') != 'read_only_new_request'
    ):
        return False
    if request.get('op') in (
        'schemas.get',
        'actions.get',
        'actions.list',
        'tasks.get',
        'tasks.list',
        'environment.describe',
        'environment.document',
        'feedback.get',
        'feedback.list',
    ):
        return True
    if request.get('op') != 'action.execute':
        return False
    from .actions.catalog import KNOWLEDGE_METHODS

    return request.get('params', {}).get('action', {}).get('method') in KNOWLEDGE_METHODS


def _collect(source, root):
    source = Path(source)
    if not source.is_absolute():
        raise ValueError('Recovery source must be an absolute run directory')
    source = source.resolve()
    root = Path(root).resolve()
    report = _read_json_retry(source / 'report.json')
    if (
        report.get('run_id') != source.name
        or report.get('status') != 'pending_request_unresolved'
    ):
        raise ValueError('Recovery source is not an unresolved stopped run')
    if (
        not report.get('cleanup', {}).get('closed')
        or report.get('cleanup', {}).get('cleanup_errors')
        or report.get('cleanup_errors')
    ):
        raise ValueError('Method cleanup is not confirmed')
    usage = report['token_usage']
    limits = report['limits']
    if (
        usage.get('complete') is not True
        or usage.get('unknown_usage_upper_bound_tokens', 0) != 0
        or not 0 <= usage['charged_tokens'] <= limits['total_tokens']
    ):
        raise ValueError('Recovery cannot renew or bypass uncertain paid accounting')
    archived = source / 'environment/session'
    ledger = _read_json_retry(archived / 'api-ledger.json')
    unknown = []
    knowledge = []
    for request_id, entry in ledger.items():
        if not _unresolved(entry.get('response', {})):
            continue
        if _legacy_knowledge_error(entry):
            knowledge.append(request_id)
        else:
            unknown.append((request_id, entry))
    if len(unknown) != 1:
        raise ValueError(
            'Exactly one unresolved passive request is required; additional uncertainty is forbidden'
        )
    request_id, entry = unknown[0]
    request = entry['request']
    response = entry['response']
    action = request.get('params', {}).get('action', {})
    if (
        request.get('op') != 'action.execute'
        or action.get('method') != 'experiment.wait'
        or action.get('side') != 'local'
        or set(action) - {'method', 'side', 'params', 'executor'}
        or action.get('executor', 'direct') != 'direct'
    ):
        raise ValueError('Only the owned synchronous passive wait can be retired')
    params = action.get('params', {})
    if (
        set(params) - {'ticks', 'timeout_seconds', 'require_natural'}
        or type(params.get('ticks')) is not int
        or params['ticks'] < 0
        or not isinstance(params.get('timeout_seconds', 60), (int, float))
        or isinstance(params.get('timeout_seconds', 60), bool)
        or not math.isfinite(params.get('timeout_seconds', 60))
        or params.get('timeout_seconds', 60) <= 0
        or ('require_natural' in params and type(params['require_natural']) is not bool)
    ):
        raise ValueError('Passive wait request parameters are not proven')
    if (
        response.get('status') != 'error'
        or response.get('execution', {}).get('state') != 'unknown'
    ):
        raise ValueError('Original wait is not the expected retained unknown error')
    session_id = _safe_id(response['session_id'])
    live = root / 'runs' / session_id
    names = (
        'api-ledger.json',
        'environment-state.json',
        'task-stream.json',
        'task-worlds.json',
        'trace.jsonl',
    )
    hashes = {}
    for name in names:
        hashes['environment/session/' + name] = _sha(archived / name)
        if (archived / name).read_bytes() != (live / name).read_bytes():
            raise ValueError('Canonical session changed after the saved source')
    state = _read_json_retry(archived / 'environment-state.json')
    stream = _read_json_retry(archived / 'task-stream.json')
    worlds = _read_json_retry(archived / 'task-worlds.json')
    instance_id = _safe_id(state['active_task_instance'])
    instance = _read_json_retry(root / 'runs/task-instances' / (instance_id + '.json'))
    active = worlds.get('active') or {}
    binding = {
        'session_id': session_id,
        'task_id': instance['task']['id'],
        'task_instance_id': instance_id,
        'profile_id': instance['task'].get('selected_profile'),
        'server_instance_id': instance['server_instance_id'],
        'workspace_world_key': instance['workspace_world_key'],
        'stream_position': stream['position'],
    }
    if worlds.get('pending') or (
        active.get('task_id'),
        active.get('instance_id'),
        active.get('world_key'),
        state.get('active_stream_position'),
        instance.get('stream_position'),
    ) != (
        binding['task_id'],
        binding['server_instance_id'],
        binding['workspace_world_key'],
        stream['position'],
        stream['position'],
    ):
        raise ValueError('Saved task/session/world/stream binding differs')
    if (
        entry.get('workspace_world_key') != binding['workspace_world_key']
        or entry.get('stream_position') != binding['stream_position']
        or instance.get('actor') != state.get('actor')
        or instance.get('server_bridge') != state.get('server_bridge')
    ):
        raise ValueError('Unknown wait belongs to another task workspace')
    manager_path = source / 'environment/host/manager.json'
    manager = _read_json_retry(manager_path)
    if (
        not manager.get('save_confirmed')
        or not manager.get('resource_released')
        or manager.get('cleanup_errors')
        or manager.get('runner_exit_code') != 0
        or manager.get('status') != 'completed'
    ):
        raise ValueError('Saved host is not cleanly released')
    if (
        (manager.get('instance_id'), manager.get('world'))
        != (binding['server_instance_id'], instance['workspace_world'])
        or manager.get('task_worlds', {}).get('active') != active
        or manager.get('task_worlds', {}).get('pending')
    ):
        raise ValueError('Saved host world does not match the original wait workspace')
    host_worlds = manager.get('task_worlds', {}).get('worlds', {})
    if set(host_worlds) != set(worlds.get('worlds', {})) or any(
        any(
            host_worlds[task_id].get(key) != world.get(key)
            for key in ('task_id', 'world_key', 'instance_id', 'world')
        )
        for task_id, world in worlds.get('worlds', {}).items()
    ):
        raise ValueError('Saved host world does not match the original wait workspace')
    selection = TaskSelection(manager['task_selection'], world_type=manager['world_type'])
    if instance['task'] != select_profile(
        selection.get(binding['task_id']), binding['profile_id']
    ):
        raise ValueError('Original task/profile definition changed')
    original_starts = []
    original_ends = []
    model_starts = set()
    model_finishes = set()
    model_unknown = []
    with (source / 'log/events.jsonl').open(encoding='utf-8') as events:
        for line in events:
            event = json.loads(line)
            call = event.get('call', {})
            kind = event.get('kind')
            if kind == 'model.start':
                model_starts.add(call['call_id'])
            if kind == 'model.finish':
                model_finishes.add(call['call_id'])
                if call.get('usage_status') != 'reported':
                    model_unknown.append(call['call_id'])
            if call.get('request', {}).get('request_id') == request_id:
                if kind == 'env.start':
                    original_starts.append(call)
                if kind == 'env.finish':
                    original_ends.append(call)
    if model_starts != model_finishes or model_unknown:
        raise ValueError('A model request is still unresolved')
    if (
        len(original_starts) != 1
        or len(original_ends) != 1
        or canonical_hash(original_ends[0]['feedback']) != canonical_hash(response)
    ):
        raise ValueError('Original wait public trace is not a unique matching completed call')
    invocations = {}
    closed = []
    outstanding = {}
    with (archived / 'trace.jsonl').open(encoding='utf-8') as trace:
        for line in trace:
            event = json.loads(line)
            if event.get('kind') == 'invocation.start':
                outstanding[event['invocation_id']] = event.get('method')
            elif event.get('kind') == 'invocation.end':
                outstanding.pop(event.get('invocation_id'), None)
            if (
                event.get('kind') == 'invocation.start'
                and event.get('method') == 'experiment.wait'
                and event.get('params') == params
            ):
                invocations[event['invocation_id']] = event
            if (
                event.get('kind') == 'invocation.end'
                and event.get('invocation_id') in invocations
            ):
                closed.append({'start': invocations[event['invocation_id']], 'end': event})
    if outstanding:
        raise ValueError(
            'An archived local invocation remains open; no background work can be retired'
        )
    if (
        len(closed) != 1
        or closed[0]['end'].get('status') != 'failed'
        or closed[0]['end'].get('error') != response.get('error', {}).get('message')
    ):
        raise ValueError('Passive wait local call has no unique terminated trace')
    source_manifest = _read_json_retry(source / 'runtime-source.json')
    current_key = 'research/environments/techmc/runtime_support.py'
    if current_key in source_manifest:
        archive_source = source / 'environment-source/code/runtime_support.py'
        expected = source_manifest[current_key]
    else:
        archive_source = source / 'environment-source/runtime_support.py'
        expected = source_manifest['research/techmc_env/runtime_support.py']
    source_proof = prove_passive_wait_source(archive_source, expected)
    prefixes = []
    for artifact in sorted((source / 'method/artifacts').glob('execution-*')):
        path = artifact / 'events.json'
        if not path.is_file():
            continue
        events = json.loads(path.read_text(encoding='utf-8'))
        matches = [
            value
            for kind, value in events
            if kind == 'techmcFeedback'
            and value.get('request', {}).get('request_id') == request_id
        ]
        if matches:
            if len(matches) != 1 or canonical_hash(matches[0]['feedback']) != canonical_hash(
                response
            ):
                raise ValueError(
                    'Preserved prefix wait feedback differs from canonical evidence'
                )
            prefixes.append(
                {
                    'artifact': artifact.name,
                    'input_sha256': _sha(artifact / 'input.json'),
                    'events_sha256': _sha(path),
                }
            )
    if len(prefixes) != 1:
        raise ValueError('Recovery requires a unique preserved execution prefix')
    hashes.update(
        {
            'report.json': _sha(source / 'report.json'),
            'environment/host/manager.json': _sha(manager_path),
            'runtime-source.json': _sha(source / 'runtime-source.json'),
            'environment-source/runtime_support.py': expected,
            'log/events.jsonl': _sha(source / 'log/events.jsonl'),
        }
    )
    return {
        'source_run': str(source),
        'source_run_id': report['run_id'],
        'source_report_sha256': hashes['report.json'],
        'source_status': report['status'],
        'retired_requests': [
            {
                'request_id': request_id,
                'request_sha256': canonical_hash(request),
                'canonical_feedback_sha256': canonical_hash(response),
                'disposition': 'retain_unknown_no_replay',
            }
        ],
        'binding': binding,
        'limits': copy.deepcopy(limits),
        'charged_tokens': usage['charged_tokens'],
        'preserved_prefix': prefixes[0],
        'next_decision_only': True,
        'continuation_boundary': 'next_decision_after_recorded_prefix',
        'provenance_hashes': hashes,
        'passive_source_proof': source_proof,
        'terminated_invocation_sha256': canonical_hash(closed[0]),
        'retained_legacy_knowledge_unknown_ids': sorted(knowledge),
        'experiment_id': manager['experiment_id'],
        'manager': manager,
        'world': instance['workspace_world'],
    }


def _physical(evidence, root, token, owner):
    manager = evidence['manager']
    port = manager['port']
    remote = for_port(port, root)
    if remote:
        return remote.retained_resume_proof(
            evidence['binding']['server_instance_id'], evidence['experiment_id'], token
        )
    state = retained_instance(
        root, port, evidence['binding']['server_instance_id'], evidence['experiment_id'], token
    )
    control = Path(root) / 'runtime/servers' / str(port)
    current = _read_json_retry(control / 'current.json')
    authenticate(current, token)
    for document in (state, current):
        if (
            document.get('owner_hash') != owner
            or document.get('experiment_id') != evidence['experiment_id']
            or document.get('instance_id') != evidence['binding']['server_instance_id']
            or document.get('world') != evidence['world']
        ):
            raise PermissionError('Saved passive-wait world ownership differs')
    for key in ('pid', 'supervisor_pid'):
        if process_alive(state.get(key)):
            raise ValueError('A saved world process remains alive')
    for path in (
        control / 'supervisor.lock',
        Path(root) / 'runtime/reservations' / (str(port) + '.json'),
        Path(root) / 'runtime/task-world-owners' / (str(port) + '.json'),
    ):
        if path.exists():
            raise ValueError('Saved world retains a lock/reservation/lease')
    return {'verified': True, 'instance': state, 'current': current, 'owner_hash': owner}


def _owner(evidence, root):
    experiment_id = _safe_id(evidence['experiment_id'])
    token = (
        (Path(root) / 'runtime/experiments' / experiment_id / 'owner.token')
        .read_text(encoding='utf-8')
        .strip()
    )
    if not token:
        raise PermissionError('Original owner credential is missing')
    return token, hashlib.sha256(token.encode()).hexdigest()


def _verify_saved_retained(evidence, root, token, owner, remote):
    """A port's unrelated occupant does not change this saved world's owner."""
    instance_id = evidence['binding']['server_instance_id']
    if remote:
        raw = remote.execute(
            "sys.path.insert(0,str(root))\n"
            "from experiment_registry import retained_resume_target\n"
            "value=retained_resume_target(root,cfg['remote_port'],cfg['instance_id'],cfg['experiment_id'],cfg['owner_token'])\n"
            "print(json.dumps(value))\n",
            {
                'instance_id': instance_id,
                'experiment_id': evidence['experiment_id'],
                'owner_token': token,
            },
        )
        value = json.loads(raw)
    else:
        value = retained_resume_target(
            root, evidence['manager']['port'], instance_id, evidence['experiment_id'], token
        )
    if (
        value.get('instance_id'),
        value.get('experiment_id'),
        value.get('owner_hash'),
        value.get('world'),
    ) != (instance_id, evidence['experiment_id'], owner, evidence['world']):
        raise PermissionError('Original retained world ownership or identity differs')
    if (
        value.get('status') != 'stopped'
        or value.get('exit_code') != 0
        or value.get('shutdown_save_confirmed') is not True
    ):
        raise ValueError('Original retained world lacks a confirmed clean saved shutdown')


def _verify_current_owner(evidence, root, token, owner):
    """Issuance is stopped-only; later verification accepts its exact new epoch."""
    manager = evidence['manager']
    remote = for_port(manager['port'], root)
    current = (
        remote.state(fresh=True)
        if remote
        else _read_json_retry(
            Path(root) / 'runtime/servers' / str(manager['port']) / 'current.json',
            missing_ok=True,
        )
    ) or {}
    live_manager = (
        Path(root) / 'runtime/experiments' / evidence['experiment_id'] / 'manager.json'
    )
    if (
        current.get('experiment_id'),
        current.get('owner_hash'),
        current.get('instance_id'),
        current.get('world'),
    ) != (
        evidence['experiment_id'],
        owner,
        evidence['binding']['server_instance_id'],
        evidence['world'],
    ):
        # A foreign port occupant is a scheduler concern, not checkpoint corruption.
        # Once our launch changes its manager, only the exact ready branch is valid.
        if _sha(live_manager) != evidence['provenance_hashes']['environment/host/manager.json']:
            raise ValueError(
                'Changed original manager cannot authorize a different connected world'
            )
        _verify_saved_retained(evidence, root, token, owner, remote)
        return
    if current.get('status') == 'stopped':
        if current.get('exit_code') != 0 or current.get('shutdown_save_confirmed') is not True:
            raise ValueError('Current saved shutdown is not confirmed')
        if _sha(live_manager) != evidence['provenance_hashes']['environment/host/manager.json']:
            raise ValueError('Saved host manager changed after the checkpoint')
    elif current.get('status') == 'ready':
        live = _read_json_retry(live_manager)
        resume = live.get('resume_from', {})
        if (
            live.get('status') != 'running'
            or resume.get('source_run') != evidence['source_run']
            or resume.get('manager_sha256')
            != evidence['provenance_hashes']['environment/host/manager.json']
            or not live.get('launch_id')
            or live['launch_id'] != current.get('launch_id')
            or live.get('instance_id') != evidence['binding']['server_instance_id']
        ):
            raise ValueError(
                'Ready host is not the authenticated startup epoch for this checkpoint'
            )
    else:
        raise ValueError(
            'Current host is neither the saved checkpoint nor its authenticated ready epoch'
        )


def issue_readonly_wait_recovery(source, receipt_path, *, root=ROOT, physical_proof=None):
    """Sign only after the original world is already cleanly saved/stopped.

    physical_proof is a trusted callable injection for offline tests, never an
    untrusted JSON assertion. Production uses native local/remote verification.
    """
    evidence = _collect(source, root)
    token, owner = _owner(evidence, root)
    if physical_proof is not None and not callable(physical_proof):
        raise TypeError('Physical proof must be a trusted verifier callable')
    proof = (
        physical_proof(evidence, token, owner)
        if physical_proof
        else _physical(evidence, root, token, owner)
    )
    if proof.get('verified') is not True:
        raise ValueError('Saved world physical proof is not verified')
    for document in (proof.get('instance', {}), proof.get('current', {})):
        if (
            (
                document.get('instance_id'),
                document.get('world'),
                document.get('owner_hash'),
                document.get('experiment_id'),
            )
            != (
                evidence['binding']['server_instance_id'],
                evidence['world'],
                owner,
                evidence['experiment_id'],
            )
            or document.get('status') != 'stopped'
            or document.get('exit_code') != 0
            or document.get('shutdown_save_confirmed') is not True
        ):
            raise PermissionError('Physical saved-world proof does not match recovery scope')
    _verify_current_owner(evidence, root, token, owner)
    body = {
        'schema': SCHEMA,
        'issued_ns': time.time_ns(),
        'owner_hash': owner,
        **evidence,
        'physical_proof': proof,
    }
    signature = hmac.new(
        token.encode(),
        json.dumps(
            body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    path = Path(receipt_path).resolve()
    if path.exists():
        raise FileExistsError('Preserve the existing recovery receipt')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        output.write(
            json.dumps(
                {**body, 'owner_hmac_sha256': signature},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        )
    return verify_readonly_wait_recovery(source, path, root=root)


def verify_readonly_wait_recovery(source, receipt_path, *, root=ROOT):
    evidence = _collect(source, root)
    token, owner = _owner(evidence, root)
    path = Path(receipt_path).resolve()
    receipt = _read_json_retry(path)
    body = {key: value for key, value in receipt.items() if key != 'owner_hmac_sha256'}
    expected = hmac.new(
        token.encode(),
        json.dumps(
            body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    signature = receipt.get('owner_hmac_sha256')
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise PermissionError('Passive wait recovery receipt authentication failed')
    if (
        body.get('schema') != SCHEMA
        or body.get('owner_hash') != owner
        or any(body.get(key) != value for key, value in evidence.items())
    ):
        raise ValueError('Passive wait recovery evidence changed or belongs to another source')
    _verify_current_owner(evidence, root, token, owner)
    fields = (
        'source_run_id',
        'source_run',
        'source_report_sha256',
        'source_status',
        'retired_requests',
        'binding',
        'limits',
        'charged_tokens',
        'preserved_prefix',
        'next_decision_only',
        'continuation_boundary',
    )
    return {
        'schema': APPROVAL_SCHEMA,
        'verified': True,
        **{key: copy.deepcopy(evidence[key]) for key in fields},
        'proof_path': str(path),
    }
