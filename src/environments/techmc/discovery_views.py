"""Versioned public documents and small discovery views; never task solutions."""

import copy
import hashlib
import importlib
import json
from pathlib import Path


def _module(name):
    return importlib.import_module((__package__ + '.' if __package__ else '') + name)


BOOTSTRAP_GUIDANCE = '''Use tasks.list detail=index to choose from the visible catalogue; tasks.get returns full requirements and task.start binds an authoritative complete task contract.
Read the complete bound task, shared_acceptance and applicable operating/resource documents before building. Follow task_completion_policy; task order is learner-selected in catalogue mode.
Use only published local Wiki/source interfaces. Wiki information pages are allowed; tutorials and external browsing are unavailable. Public procedure documents are the same source material for every method.
Use evaluation mode auto unless explicitly needed otherwise. Required structural review cannot be skipped and unavailable evidence is inconclusive, never a pass.
The experiment host owns allocation, ports, heartbeat and shutdown. Do not reset or replace a world without explicit authorization. Preserve exact request IDs and payloads for uncertain operations; never blindly replay a mutation.
Use environment.document with a document reference's id and sha256 to read its complete rules or procedure source. Documents are versioned. Summary/reference views do not mean missing evidence is empty or that a prior document remains in model context.'''


def _hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _store(environment):
    if not hasattr(environment, '_public_documents'):
        path = Path(environment.session.directory) / 'public-documents.json'
        store = (
            json.loads(path.read_text(encoding='utf-8'))
            if path.is_file()
            else {'versions': {}, 'current': {}}
        )
        if not isinstance(store.get('versions'), dict) or not isinstance(
            store.get('current'), dict
        ):
            raise ValueError('Invalid public document archive')
        for key, value in store['versions'].items():
            if (
                key != value['id'] + '@' + value['sha256']
                or _hash(value['content']) != value['sha256']
            ):
                raise ValueError('Public document archive content hash mismatch')
        environment._public_documents = store
    return environment._public_documents


def _reference(identifier, digest):
    return {
        'id': identifier,
        'sha256': digest,
        'read': {'op': 'environment.document', 'params': {'id': identifier, 'sha256': digest}},
    }


def _publish(environment, identifier, content):
    store = _store(environment)
    digest = _hash(content)
    store['versions'][identifier + '@' + digest] = {
        'id': identifier,
        'sha256': digest,
        'content': copy.deepcopy(content),
    }
    store['current'][identifier] = digest
    return _reference(identifier, digest)


def _resource_content(environment, description):
    resource = copy.deepcopy(description.get('resource_policy', {}))
    if resource.get('view') == 'reference':
        original = environment.ledger.get(resource.get('feedback_ref'), {}).get('response', {})
        resource = copy.deepcopy(original.get('result', {}).get('resource_policy', {}))
    if not resource.get('agent_instructions'):
        resource = _module('resource_policy').describe(environment)
    resource.pop('recall', None)
    return resource


def manifest(environment, description):
    """Project a description while retaining every referenced original document."""
    with environment._ledger_lock:
        return _manifest(environment, description)


def _manifest(environment, description):
    result = copy.deepcopy(description)
    guidance = description['agent_guidance']
    resource = _resource_content(environment, description)
    documents = {
        'operating': _publish(environment, 'rules.operating', guidance['operating_rules']),
        'resources': _publish(environment, 'rules.resources', resource),
        'completion': _publish(
            environment, 'rules.completion', description['task_completion_policy']
        ),
        'acceptance': _publish(
            environment, 'rules.acceptance', description['shared_acceptance']
        ),
    }
    procedures = []
    for procedure in guidance.get('public_procedures', []):
        reference = _publish(environment, 'procedure.' + procedure['id'], procedure)
        procedures.append(
            {
                k: copy.deepcopy(procedure[k])
                for k in ('id', 'source', 'sha256')
                if k in procedure
            }
        )
        procedures[-1]['document'] = reference
    result['view'] = 'discovery_manifest'
    result['view_version'] = 'discovery-manifest-v1'
    result['schemas'] = {
        'available': list(_module('contracts').SCHEMAS),
        'details_op': 'schemas.get',
        'operation_parameter': 'Use name=request and operation=<op> for one request contract',
    }
    result['agent_guidance'] = {
        'operating_rules': BOOTSTRAP_GUIDANCE,
        'public_procedures': procedures,
        'source': 'techmc_env',
        'version': 'manifest-v1',
    }
    result['documents'] = documents
    result['required_documents'] = ['rules.operating', 'rules.resources']
    result['resource_policy'] = {
        k: copy.deepcopy(v) for k, v in resource.items() if k != 'agent_instructions'
    }
    result['resource_policy']['document'] = documents['resources']
    result['feedback_access'] = {
        **result.get('feedback_access', {}),
        'documents': 'environment.document with id and optional exact sha256',
        'status': 'environment.status returns current session/selection/progress/resource facts',
        'region_sections': ['basic', 'equipment', 'cooking', 'sources', 'activity'],
        'fuel_registry': 'registry.get name=fuels; exact observed versions use sha256',
    }
    result.setdefault('knowledge_access', {})['service_reads'] = [
        'environment.document',
        'environment.status',
        'schemas.get',
        'actions.get',
        'feedback.get',
        'feedback.list',
        'registry.get',
    ]
    result['task_catalogue'] = {
        'index': {'op': 'tasks.list', 'params': {'detail': 'index'}},
        'details': {'op': 'tasks.get', 'params': {'task_id': '<visible task id>'}},
        'full': {'op': 'tasks.list', 'params': {'detail': 'full'}},
    }
    _module('runtime_support').atomic_json(
        Path(environment.session.directory) / 'public-documents.json', _store(environment)
    )
    return result


def document(environment, params, request_id):
    with environment._ledger_lock:
        return _document(environment, params, request_id)


def _document(environment, params, request_id):
    store = _store(environment)
    identifier = params['id']
    digest = params.get('sha256')
    if digest is None or identifier not in store['current']:
        description = environment._dispatch(
            'environment.describe', {'detail': 'summary'}, request_id
        )
        manifest(environment, description)
    digest = digest or store['current'].get(identifier)
    key = identifier + '@' + str(digest)
    if key not in store['versions']:
        raise _module('adapters.bridge').OperationNotStarted(
            ValueError('Unknown public document or version: ' + identifier)
        )
    return copy.deepcopy(store['versions'][key])


def status(environment):
    """Current mutable facts; no procedure texts, API catalogue or rule archive."""
    selection = (
        {'mode': 'task_stream', **environment.task_stream.public_state()}
        if environment.task_stream
        else {
            **environment.task_selection.metadata,
            'configured_by': environment.task_selection_source,
        }
    )
    resource = _module('resource_policy').describe(environment)
    return {
        'view': 'environment_status',
        'protocol_version': _module('contracts').VERSION,
        'session_id': environment.session.run_id,
        'actor': environment.actor,
        'server_bridge': environment.session.server.url,
        'evaluating': environment.evaluating,
        'task_selection': copy.deepcopy(selection),
        'task_progress': copy.deepcopy(environment.task_progress),
        'resource_policy': {
            k: copy.deepcopy(resource[k])
            for k in (
                'metadata_status',
                'configured_limits',
                'evaluation_limits',
                'health_query',
            )
        },
    }


def task_index(tasks):
    keys = (
        'id',
        'title',
        'difficulty',
        'world_type',
        'natural_source',
        'production_kind',
        'curriculum',
        'acceptance',
        'actor_policy',
        'calibration_status',
        'task_hash',
        'mandatory_review_checks',
        'machine_limits',
        'shulker_io',
        'inventory_batch',
        'primary_products',
    )
    return [
        {
            **{k: copy.deepcopy(task[k]) for k in keys if k in task},
            'world_type': task.get('world_type', 'void'),
            'task_view': 'index',
            'product_profile_count': len(task.get('product_profiles', [])),
            'requirements_ref': {'op': 'tasks.get', 'params': {'task_id': task['id']}},
        }
        for task in tasks
    ]


def operation_schema(name, operation):
    contracts = _module('contracts')
    if name != 'request':
        raise ValueError('operation is supported only for the request schema')
    if operation not in contracts.OP_PARAMS:
        raise ValueError('Unknown operation: ' + str(operation))
    result = copy.deepcopy(contracts.SCHEMAS['request'])
    result.pop('allOf', None)
    result['properties']['op'] = {'const': operation}
    result['properties']['params'] = copy.deepcopy(contracts.OP_PARAMS[operation])
    return result
