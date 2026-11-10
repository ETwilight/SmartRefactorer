"""Lossless presentation partitions for region telemetry and fuel registries.

Canonical region observations remain in the request ledger. These views do not
change acquisition or evaluator inputs and never manufacture empty telemetry.
"""

import copy
import hashlib
import importlib
import json
import uuid
from contextlib import nullcontext

SECTIONS = {
    'equipment': ('survival_equipment', 'component_item_counts', 'item_components_version'),
    'cooking': ('cooking_ledger', 'furnace_states'),
    'sources': (
        'natural_source_policy',
        'budding_positions',
        'harvest_ledger',
        'wither_source_ledger',
    ),
    'activity': ('inflight_block_counts', 'unticking_blocks'),
}


def _module(name):
    return importlib.import_module((__package__ + '.' if __package__ else '') + name)


def content_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _fuel_values(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError('Fuel registry telemetry is unavailable or malformed')
    return copy.deepcopy(value)


def _recall(request_id, fields):
    return {'op': 'feedback.get', 'params': {'request_id': request_id, 'fields': fields}}


def is_region(feedback):
    result = feedback.get('result')
    return (
        feedback.get('status') == 'completed'
        and isinstance(result, dict)
        and result.get('status', 'ok') == 'ok'
        and result.get('method') == 'observe.region'
        and isinstance(result.get('result'), dict)
    )


def present_region(feedback, sections=None):
    """Return basic data plus explicitly selected sections and exact recall links."""
    sections = ['basic'] if sections is None else list(sections)
    if not sections or len(set(sections)) != len(sections):
        raise ValueError('Region sections must be nonempty and unique')
    unknown = set(sections) - {'basic', *SECTIONS}
    if unknown:
        raise ValueError('Unknown region sections: ' + ', '.join(sorted(unknown)))
    result = copy.deepcopy(feedback)
    envelope = result['result']
    full = feedback['result']['result']
    request_id = feedback['request_id']
    partitioned = {field for fields in SECTIONS.values() for field in fields} | {
        'fuel_item_ids'
    }
    body = {key: copy.deepcopy(value) for key, value in full.items() if key not in partitioned}
    selected = set(sections)
    section_index = {}
    omitted = []
    for name, fields in SECTIONS.items():
        present = [field for field in fields if field in full]
        included = name in selected
        section_index[name] = {
            'status': ('included' if included else 'omitted') if present else 'not_in_sample',
            'fields': present,
            'read': (
                _recall(request_id, ['result.result.' + field for field in present])
                if present
                else None
            ),
        }
        if included:
            body.update({field: copy.deepcopy(full[field]) for field in present})
        else:
            omitted.extend(present)
    if 'fuel_item_ids' in full:
        values = _fuel_values(full['fuel_item_ids'])
        digest = content_hash(values)
        body['fuel_registry'] = {
            'name': 'fuels',
            'sha256': digest,
            'read': {'op': 'registry.get', 'params': {'name': 'fuels', 'sha256': digest}},
            'source': _recall(request_id, ['result.result.fuel_item_ids']),
        }
        omitted.append('fuel_item_ids')
    body['observation_view'] = {
        'kind': 'region_sections',
        'included_sections': sections,
        'omitted_fields': omitted,
        'sections': section_index,
        'full': _recall(request_id, ['result.result']),
        'semantics': 'Omitted telemetry is retained, not empty or known absent. Basic counts/loading facts remain visible.',
    }
    envelope['result'] = body
    result['feedback_ref'] = request_id
    result['response_view'] = {'detail': 'summary', 'sections': sections}
    return result


def _binding(environment):
    current = _module('server_recovery').binding(environment)
    return {
        'session_id': environment.session.run_id,
        'server_instance_id': current.get('instance_id'),
        'server_world': current.get('world'),
    }


def _same_world(record, world):
    return (
        record.get('session_id') == world['session_id']
        and record.get('server_instance_id') == world['server_instance_id']
        and record.get('server_world') == world['server_world']
    )


def _registry_records(environment, world):
    records = []
    path = environment.session.directory / 'registries.json'
    if path.is_file():
        saved = json.loads(path.read_text(encoding='utf-8'))
        records.extend(
            record for record in saved.get('records', []) if _same_world(record, world)
        )
    for rid, entry in list(environment.ledger.items()):
        response = entry.get('response', {})
        if not is_region(response):
            continue
        if (
            entry.get('server_instance_id') != world['server_instance_id']
            or entry.get('server_world') != world['server_world']
        ):
            continue
        payload = response['result']['result']
        if 'fuel_item_ids' not in payload:
            continue
        values = _fuel_values(payload['fuel_item_ids'])
        records.append(
            {
                **world,
                'name': 'fuels',
                'sha256': content_hash(values),
                'content': values,
                'source_request_id': rid,
                'source_method': 'observe.region',
                'sampled_for_registry': False,
            }
        )
    return records


def _save_record(environment, record):
    path = environment.session.directory / 'registries.json'
    with getattr(environment, '_ledger_lock', nullcontext()):
        saved = (
            json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'records': []}
        )
        identity = (
            record['session_id'],
            record['server_instance_id'],
            record['server_world'],
            record['sha256'],
        )
        if not any(
            (r['session_id'], r['server_instance_id'], r['server_world'], r['sha256'])
            == identity
            for r in saved['records']
        ):
            saved['records'].append(record)
            _module('runtime_support').atomic_json(path, saved)


def registry_get(environment, params, request_id):
    """Read an exact observed fuel registry in the current world binding.

    A versionless request samples the server once to observe its current registry;
    a versioned request never substitutes a new registry or crosses world bindings.
    """
    if params.get('name') != 'fuels':
        raise ValueError('Unknown registry name')
    if environment.evaluating:
        raise ValueError('Registry queries are unavailable during an acceptance window')
    world = _binding(environment)
    digest = params.get('sha256')
    if digest:
        if world['server_instance_id'] is None:
            raise ValueError(
                'Registry world identity is unavailable; read the exact original sample with feedback.get'
            )
        matches = [r for r in _registry_records(environment, world) if r['sha256'] == digest]
        if not matches:
            raise ValueError('Registry version was not observed in the current world binding')
        record = matches[-1]
        if content_hash(_fuel_values(record['content'])) != digest:
            raise ValueError('Registry contents do not match their advertised version')
    else:
        child_id = 'registry-' + str(uuid.uuid4())
        action = {
            'method': 'observe.region',
            'side': 'server',
            'executor': 'direct',
            'phase': 'diagnose',
            'params': {'dimension': 'minecraft:overworld', 'min': [0, 0, 0], 'max': [0, 0, 0]},
        }
        sample = environment._primitive(action, child_id)
        if (
            not isinstance(sample, dict)
            or sample.get('status') != 'ok'
            or not isinstance(sample.get('result'), dict)
        ):
            raise ValueError('Fuel registry sampling did not return a completed observation')
        after = _binding(environment)
        if world != after:
            raise ValueError(
                'World binding changed during registry sampling; no registry was published'
            )
        values = _fuel_values(sample['result'].get('fuel_item_ids'))
        record = {
            **world,
            'name': 'fuels',
            'sha256': content_hash(values),
            'content': values,
            'source_request_id': child_id,
            'source_method': 'observe.region',
            'parent_request_id': request_id,
            'sampled_for_registry': True,
        }
    _save_record(environment, record)
    return {
        'name': 'fuels',
        'sha256': record['sha256'],
        'content': copy.deepcopy(record['content']),
        'world_binding': {
            'server_instance_id': world['server_instance_id'],
            'identity_verified': world['server_instance_id'] is not None,
        },
        'provenance': {
            key: record[key]
            for key in (
                'source_request_id',
                'source_method',
                'sampled_for_registry',
                'parent_request_id',
            )
            if key in record
        },
        'semantics': 'Exact observed version; a versionless query samples the current registry.',
    }
