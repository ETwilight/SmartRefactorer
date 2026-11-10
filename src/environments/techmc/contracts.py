"""Versioned JSON contracts shared by CLI, Python controllers and evaluators."""

from pathlib import Path
import json
from jsonschema import Draft202012Validator

VERSION = '1.0'
INTERFACE_LANGUAGE = 'en'
ROOT = Path(__file__).resolve().parents[2] / "assets/techmc"
ID = {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,128}$'}
ITEM = {'type': 'string', 'pattern': '^minecraft:[a-z0-9_/]+$'}
INT32 = {'type': 'integer', 'minimum': -2147483648, 'maximum': 2147483647}
POS = {'type': 'array', 'items': INT32, 'minItems': 3, 'maxItems': 3}
VEC = {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}
DIM = {
    'type': 'string',
    'enum': ['minecraft:overworld', 'minecraft:the_nether', 'minecraft:the_end'],
}


def obj(properties, required=None):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(properties) if required is None else required,
        'additionalProperties': False,
    }


CONTAINER = {
    'oneOf': [
        obj({'position': POS, 'dimension': DIM}, ['position']),
        obj(
            {'entity_uuid': {'type': 'string', 'format': 'uuid'}, 'dimension': DIM},
            ['entity_uuid'],
        ),
    ]
}
BOUNDS = obj({'min': POS, 'max': POS})
OUTPUT_TARGET = obj(
    {
        'min_net_items': {'type': 'integer', 'minimum': 1},
        'min_per_game_hour': {'type': 'number', 'exclusiveMinimum': 0},
    }
)
MATERIAL_CONVERSION = obj(
    {
        'input': ITEM,
        'output': ITEM,
        'input_units': {'type': 'integer', 'minimum': 1},
        'output_units': {'type': 'integer', 'minimum': 1},
    }
)
ACTION = obj(
    {
        'method': ID,
        'side': {'enum': ['client', 'server', 'local']},
        'params': {'type': 'object'},
        'executor': {'enum': ['direct', 'cli', 'execution_agent']},
        'wait': {'type': 'boolean'},
        'phase': {'enum': ['setup', 'build', 'warmup', 'measure', 'diagnose']},
    },
    ['method', 'side', 'params'],
)
SUBMISSION = obj(
    {
        'task_instance_id': ID,
        'dimension': DIM,
        'bounds': BOUNDS,
        'regions': {
            'type': 'array',
            'maxItems': 16,
            'items': obj({'dimension': DIM, 'bounds': BOUNDS}),
        },
        'mechanism': {'type': 'string', 'minLength': 20, 'maxLength': 12000},
        'inspection_points': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 128,
            'items': obj(
                {
                    'role': ID,
                    'position': POS,
                    'purpose': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                    'dimension': DIM,
                },
                ['role', 'position', 'purpose'],
            ),
        },
        'outputs': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 16,
            'items': obj(
                {
                    'product': ITEM,
                    'containers': {
                        'type': 'array',
                        'minItems': 0,
                        'maxItems': 128,
                        'items': CONTAINER,
                    },
                    'collection_path': {
                        'type': 'array',
                        'minItems': 1,
                        'maxItems': 64,
                        'items': POS,
                    },
                }
            ),
        },
        'inputs': {'type': 'array', 'maxItems': 128, 'items': CONTAINER},
        'variant_outputs': {
            'type': 'object',
            'propertyNames': ID,
            'additionalProperties': {
                'type': 'array',
                'minItems': 1,
                'maxItems': 128,
                'items': CONTAINER,
            },
        },
        'operation': obj(
            {
                'afk_position': VEC,
                'driver': {'enum': ['none', 'attack', 'use', 'attack_and_use']},
                'target': {'anyOf': [POS, {'type': 'null'}]},
                'aim_point': {'anyOf': [VEC, {'type': 'null'}]},
                'tool': {'anyOf': [ITEM, {'type': 'null'}]},
                'planting_footprint': {'enum': ['single', '2x2']},
                'tool_components': {'type': 'object'},
                'offhand_item': ITEM,
                'startup_steps': {'type': 'string', 'maxLength': 4000},
                'consumables': {
                    'type': 'array',
                    'maxItems': 32,
                    'items': obj({'item': ITEM, 'source': CONTAINER}),
                },
            },
            [
                'afk_position',
                'driver',
                'target',
                'aim_point',
                'tool',
                'startup_steps',
                'consumables',
            ],
        ),
        'claims': {'type': 'string', 'maxLength': 8000},
    },
    [
        'task_instance_id',
        'dimension',
        'bounds',
        'mechanism',
        'inspection_points',
        'outputs',
        'inputs',
        'operation',
        'claims',
    ],
)
CONTROL = obj(
    {
        'position': POS,
        'dimension': DIM,
        'action': {'enum': ['set_lever', 'press_button']},
        'powered': {'type': 'boolean'},
    },
    ['position', 'action'],
)
SUBMISSION['properties']['machine_modes'] = {
    'type': 'object',
    'propertyNames': ID,
    'additionalProperties': obj(
        {
            'controls': {'type': 'array', 'items': CONTROL, 'minItems': 1, 'maxItems': 64},
            'operation': SUBMISSION['properties']['operation'],
        },
        ['controls'],
    ),
}
SUBMISSION['properties']['review_volumes'] = {
    'type': 'array',
    'minItems': 1,
    'maxItems': 16,
    'items': obj(
        {
            'dimension': DIM,
            'bounds': BOUNDS,
            'purpose': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        }
    ),
}
SUBMISSION['properties']['start_controls'] = {
    'type': 'array',
    'minItems': 1,
    'maxItems': 64,
    'items': CONTROL,
}
REQUEST = obj(
    {
        'protocol_version': {'const': VERSION},
        'request_id': ID,
        'op': {
            'enum': [
                'environment.describe',
                'actions.list',
                'tasks.list',
                'tasks.get',
                'task.start',
                'action.execute',
                'submission.validate',
                'evaluation.run',
                'evaluation.get',
                'session.close',
            ]
        },
        'params': {'type': 'object'},
    }
)
CHECK = obj(
    {
        'id': ID,
        'status': {'enum': ['pass', 'fail', 'inconclusive', 'not_applicable']},
        'message': {'type': 'string'},
        'evidence': {'type': 'array', 'items': {'type': 'string'}},
    }
)
FAULT = obj({'type': {'type': 'string'}, 'message': {'type': 'string'}})
PROGRESS_RECORD = obj(
    {
        'state': {'enum': ['active', 'deferred', 'abandoned', 'passed']},
        'task_id': ID,
        'reason': {'type': 'string'},
        'evaluation_id': ID,
        'source': {'type': 'string'},
    },
    ['state'],
)
INFRASTRUCTURE = obj(
    {
        'status': {
            'enum': [
                'request_uncertain',
                'server_or_transport_unavailable',
                'server_unavailable',
                'recovered',
                'recovery_failed',
                'recovery_error',
                'not_confirmed_stopped',
                'already_available',
                'unmanaged',
                'ownership_mismatch',
                'measurement_interrupted',
                'persistence_failed',
                'review_unavailable',
            ]
        },
        'recovered': {'type': 'boolean'},
        'reset_available': {'type': 'boolean'},
        'same_world': {'type': ['boolean', 'null']},
        'measurement_valid': {'type': 'boolean'},
        'incident_id': ID,
        'reason': {'type': 'string'},
        'stage': {
            'enum': ['request', 'measurement', 'recovery', 'persistence', 'cleanup', 'review']
        },
        'fault': FAULT,
        'recovery_error': FAULT,
        'state_error': FAULT,
        'persistence_error': FAULT,
        'process_state': {'type': 'string'},
        'note': {'type': 'string'},
        'action_outcome': {'type': 'string'},
        'uncertain_action': {'type': 'string'},
        'clock_and_actors': {'type': 'string'},
        'task_instances': {'type': 'string'},
        'old_instance_id': {'anyOf': [ID, {'type': 'null'}]},
        'instance_id': {'anyOf': [ID, {'type': 'null'}]},
        'old_world': {'type': ['string', 'null']},
        'world': {'type': ['string', 'null']},
        'passed_tasks': {'type': 'object', 'additionalProperties': PROGRESS_RECORD},
        'reset_op': {
            'anyOf': [
                obj(
                    {
                        'op': {'const': 'server.recover'},
                        'params': obj({'reset_world': {'const': True}}),
                    }
                ),
                {'type': 'null'},
            ]
        },
    },
    ['status'],
)
EXECUTION = obj(
    {
        'state': {'enum': ['not_started', 'completed', 'running', 'unknown']},
        'retry': {
            'enum': [
                'none',
                'same_request_only',
                'observe_before_new_request',
                'read_only_new_request',
                'new_evaluation',
            ]
        },
        'next_steps': {'type': 'array', 'items': {'type': 'string'}},
    }
)
REPORT = obj(
    {
        'protocol_version': {'const': VERSION},
        'evaluation_id': ID,
        'task_instance_id': ID,
        'task_id': ID,
        'task_hash': {'type': 'string'},
        'submission_hash': {'type': 'string'},
        'verdict': {'enum': ['pass', 'fail', 'inconclusive', 'measured']},
        'goal_passed': {'type': ['boolean', 'null']},
        'check_pass_fraction': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'checks': {'type': 'array', 'items': CHECK},
        'metrics': {'type': 'object'},
        'ai_review': {'type': ['object', 'null']},
        'evidence_path': {'type': 'string'},
        'cleanup_errors': {'type': 'array', 'items': {'type': 'string'}},
        'infrastructure': INFRASTRUCTURE,
        'evaluation_mode': obj(
            {
                'requested': {'enum': ['auto', 'deterministic', 'hybrid']},
                'effective': {'enum': ['deterministic', 'hybrid']},
                'required_review_checks': {'type': 'array', 'items': ID, 'uniqueItems': True},
            }
        ),
    },
    [
        'protocol_version',
        'evaluation_id',
        'task_instance_id',
        'task_id',
        'task_hash',
        'submission_hash',
        'verdict',
        'goal_passed',
        'check_pass_fraction',
        'checks',
        'metrics',
        'ai_review',
        'evidence_path',
        'cleanup_errors',
    ],
)
REPORT['allOf'] = [
    {
        'if': {'properties': {'verdict': {'const': 'inconclusive'}}},
        'then': {'properties': {'goal_passed': {'type': 'null'}}},
    },
    {
        'if': {
            'required': ['infrastructure'],
            'properties': {
                'infrastructure': {
                    'required': ['measurement_valid'],
                    'properties': {'measurement_valid': {'const': False}},
                }
            },
        },
        'then': {
            'properties': {
                'verdict': {'const': 'inconclusive'},
                'goal_passed': {'type': 'null'},
            }
        },
    },
]
OP_PARAMS = {
    'environment.describe': obj({}),
    'actions.list': obj({}),
    'tasks.list': obj(
        {
            'difficulty': {'enum': ['easy', 'medium', 'hard', 'expert']},
            'world_type': {'enum': ['void', 'natural']},
        },
        [],
    ),
    'tasks.get': obj({'task_id': ID}),
    'task.start': obj({'task_id': ID, 'profile_id': ID}, ['task_id']),
    'action.execute': obj({'action': ACTION}),
    'submission.validate': obj(
        {'submission': SUBMISSION, 'preflight': {'type': 'boolean'}}, ['submission']
    ),
    'evaluation.run': obj(
        {
            'submission': SUBMISSION,
            'mode': {'enum': ['auto', 'deterministic', 'hybrid'], 'default': 'auto'},
            'tick_mode': {'enum': ['natural', 'sprint']},
        },
        ['submission'],
    ),
    'evaluation.get': obj({'evaluation_id': ID}),
    'session.close': obj({}),
}
RESPONSE_VIEW = obj(
    {
        'detail': {'enum': ['full', 'summary', 'reference']},
        'fields': {
            'type': 'array',
            'items': {'type': 'string', 'minLength': 1},
            'minItems': 1,
            'maxItems': 64,
        },
        'sections': {
            'type': 'array',
            'items': {'enum': ['basic', 'equipment', 'cooking', 'sources', 'activity']},
            'minItems': 1,
            'uniqueItems': True,
        },
    },
    [],
)
RESPONSE_VIEW['not'] = {'required': ['fields', 'sections']}
REQUEST['properties']['response'] = RESPONSE_VIEW
OP_PARAMS.update(
    {
        'actions.get': obj({'method': ID}),
        'schemas.get': obj(
            {'name': {'type': 'string'}, 'operation': {'type': 'string'}}, ['name']
        ),
        'environment.document': obj(
            {'id': ID, 'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}, ['id']
        ),
        'environment.status': obj({}),
        'registry.get': obj(
            {
                'name': {'enum': ['fuels']},
                'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            },
            ['name'],
        ),
        'feedback.get': obj(
            {
                'request_id': ID,
                'fields': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                'path': {'type': 'string'},
                'offset': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 256},
            },
            ['request_id'],
        ),
        'feedback.list': obj(
            {
                'offset': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 256},
                'op': {'type': 'string'},
            },
            [],
        ),
        'task.progress': obj(
            {
                'task_id': ID,
                'state': {'enum': ['deferred', 'abandoned', 'active']},
                'reason': {'type': 'string', 'maxLength': 4000},
            },
            ['task_id', 'state', 'reason'],
        ),
        'server.recover': obj(
            {
                'reset_world': {'type': 'boolean'},
                'reason': {'type': 'string', 'maxLength': 4000},
            },
            [],
        ),
    }
)
OP_PARAMS['environment.describe'] = obj(
    {'detail': {'enum': ['full', 'summary', 'manifest']}}, []
)
OP_PARAMS['tasks.list']['properties']['detail'] = {'enum': ['full', 'index']}
OP_PARAMS['actions.list'] = obj(
    {
        'detail': {'enum': ['full', 'index']},
        'category': {'type': 'string'},
        'prefix': {'type': 'string'},
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 256},
    },
    [],
)
REQUEST['properties']['op']['enum'] = list(OP_PARAMS)
REQUEST['allOf'] = [
    {'if': {'properties': {'op': {'const': op}}}, 'then': {'properties': {'params': schema}}}
    for op, schema in OP_PARAMS.items()
]
RESPONSE = obj(
    {
        'protocol_version': {'const': VERSION},
        'request_id': ID,
        'session_id': ID,
        'status': {'enum': ['completed', 'running', 'error', 'pending_or_unknown']},
        'result': {},
        'error': {
            'anyOf': [
                obj({'type': {'type': 'string'}, 'message': {'type': 'string'}}),
                {'type': 'null'},
            ]
        },
        'actor_state': {'type': 'object'},
        'feedback_ref': ID,
        'response_view': RESPONSE_VIEW,
        'infrastructure': INFRASTRUCTURE,
        'execution': EXECUTION,
        'source_request_id': ID,
        'source_execution': EXECUTION,
    },
    ['protocol_version', 'request_id', 'session_id', 'status', 'result', 'error'],
)
TASK = obj(
    {
        'id': ID,
        'title': {'type': 'string'},
        'mode': {'enum': ['threshold', 'open_efficiency']},
        'primary_products': {
            'anyOf': [
                {'type': 'array', 'items': ITEM, 'minItems': 1, 'uniqueItems': True},
                {'type': 'null'},
            ]
        },
        'required_roles': {'type': 'array', 'items': ID, 'minItems': 1, 'uniqueItems': True},
        'mechanism_groups': {
            'type': 'array',
            'items': {'type': 'array', 'items': ITEM, 'minItems': 1},
        },
        'measurement': obj(
            {
                'warmup_ticks': {'type': 'integer', 'minimum': 0},
                'window_ticks': {'type': 'integer', 'minimum': 1},
                'min_net_items': {'type': ['integer', 'null'], 'minimum': 0},
                'min_per_game_hour': {'type': ['number', 'null'], 'minimum': 0},
                'random_tick_speed': {'type': 'integer', 'minimum': 0},
                'mode': {'enum': ['natural', 'sprint']},
                'allowed_modes': {
                    'type': 'array',
                    'items': {'enum': ['natural', 'sprint']},
                    'minItems': 1,
                    'uniqueItems': True,
                },
                'product_targets': {
                    'type': 'object',
                    'propertyNames': ITEM,
                    'additionalProperties': OUTPUT_TARGET,
                },
                'aggregate_targets': {
                    'type': 'array',
                    'items': obj(
                        {
                            'id': ID,
                            'products': {
                                'type': 'array',
                                'items': ITEM,
                                'minItems': 1,
                                'uniqueItems': True,
                            },
                            'target': OUTPUT_TARGET,
                        }
                    ),
                },
                'segments': {'type': 'integer', 'minimum': 1, 'maximum': 12},
            },
            [
                'warmup_ticks',
                'window_ticks',
                'min_net_items',
                'min_per_game_hour',
                'random_tick_speed',
                'mode',
                'allowed_modes',
            ],
        ),
        'storage': obj(
            {
                'reserved_stack_slots_per_primary': {'type': 'integer', 'minimum': 1},
                'headroom_ticks': {'type': 'integer', 'minimum': 0},
                'min_free_stack_slots': {'type': 'integer', 'minimum': 0},
                'destination': {'enum': ['containers', 'evaluator_inventory']},
            },
            ['reserved_stack_slots_per_primary', 'headroom_ticks', 'min_free_stack_slots'],
        ),
        'allowed_driver_tools': {'type': 'array', 'items': ITEM},
        'description': {'type': 'string'},
        'forbidden_blocks': {'type': 'array', 'items': ITEM, 'uniqueItems': True},
        'sources': {'type': 'array', 'items': {'type': 'string', 'format': 'uri'}},
        'suite': {'enum': ['production_curriculum']},
        'difficulty': {'enum': ['easy', 'medium', 'hard', 'expert']},
        'world_type': {'enum': ['void', 'natural']},
        'natural_source': {'enum': ['monument', 'trial_chambers', 'bedrock', 'amethyst_geode']},
        'shulker_io': obj({'output': {'const': True}, 'input': {'type': 'boolean'}}),
        'mode_requirements': {'type': 'object'},
        'mode_trials': {
            'type': 'array',
            'minItems': 1,
            'items': obj(
                {
                    'id': ID,
                    'products': {'type': 'array', 'items': ITEM, 'minItems': 1, 'maxItems': 16},
                    'kind': {'enum': ['continuous', 'batch']},
                    'quantity': {'type': 'integer', 'minimum': 1},
                    'potion': ITEM,
                },
                ['id', 'products', 'kind'],
            ),
        },
        'mandatory_review_checks': {'type': 'array', 'items': ID, 'uniqueItems': True},
        'order_window_ticks': {'type': 'integer', 'minimum': 200},
        'order_settle_ticks': {'type': 'integer', 'minimum': 20},
        'material_driver_items': {'type': 'array', 'items': ITEM, 'uniqueItems': True},
        'allowed_offhand_items': {'type': 'array', 'items': ITEM, 'uniqueItems': True},
        'brew_orders': {
            'type': 'array',
            'items': obj(
                {
                    'id': ID,
                    'item': ITEM,
                    'potion': ITEM,
                    'count': {'type': 'integer', 'minimum': 1},
                }
            ),
        },
        'variant_targets': {
            'type': 'array',
            'items': obj(
                {
                    'id': ID,
                    'item': ITEM,
                    'components': {'type': 'object'},
                    'target': OUTPUT_TARGET,
                    'separate_storage': {'type': 'boolean'},
                }
            ),
        },
        'production_kind': {'enum': ['renewable', 'processing', 'integrated']},
        'operating_requirements': {
            'type': 'array',
            'items': {'type': 'string', 'minLength': 1},
            'minItems': 1,
        },
        'product_profiles': {
            'type': 'array',
            'minItems': 1,
            'items': obj(
                {
                    'id': ID,
                    'products': {
                        'type': 'array',
                        'items': ITEM,
                        'minItems': 1,
                        'maxItems': 16,
                        'uniqueItems': True,
                    },
                    'processing_inputs': {
                        'type': 'array',
                        'items': ITEM,
                        'minItems': 1,
                        'uniqueItems': True,
                    },
                    'earth_inputs': {
                        'type': 'array',
                        'items': ITEM,
                        'minItems': 1,
                        'uniqueItems': True,
                    },
                    'required_recipe_observers': {
                        'type': 'array',
                        'items': {'enum': ['cooking', 'crafting', 'brewing']},
                        'minItems': 1,
                        'uniqueItems': True,
                    },
                    'required_checks': {'type': 'array', 'items': ID, 'uniqueItems': True},
                    'inventory_batch': obj({'input_item': ITEM, 'count': {'const': 36}}),
                },
                ['id', 'products'],
            ),
        },
        'processing_inputs': {
            'type': 'array',
            'items': ITEM,
            'minItems': 1,
            'uniqueItems': True,
        },
        'earth_inputs': {'type': 'array', 'items': ITEM, 'minItems': 1, 'uniqueItems': True},
        'required_recipe_observers': {
            'type': 'array',
            'items': {'enum': ['cooking', 'crafting', 'brewing']},
            'minItems': 1,
            'uniqueItems': True,
        },
        'material_conversion': MATERIAL_CONVERSION,
        'material_growth': {'type': 'array', 'items': ITEM, 'minItems': 1, 'uniqueItems': True},
        'block_output_balance': {'type': 'boolean'},
        'machine_limits': {
            'oneOf': [
                obj(
                    {
                        'max_size': {
                            'type': 'array',
                            'items': {'type': 'integer', 'minimum': 1},
                            'minItems': 3,
                            'maxItems': 3,
                        },
                        'one_axis_max_size': {'type': 'integer', 'minimum': 1},
                    },
                    ['max_size'],
                ),
                obj(
                    {
                        'max_subchunks_per_axis': {'type': 'integer', 'minimum': 1},
                        'furnaces': {'type': 'integer', 'minimum': 1},
                        'min_items_per_furnace_per_game_hour': {
                            'type': 'number',
                            'exclusiveMinimum': 0,
                        },
                    }
                ),
            ]
        },
        'resource_balance_include_blocks': {'type': 'boolean'},
        'resource_balance_min_increase': {'type': 'integer', 'minimum': 1},
        'single_input_items': {
            'type': 'array',
            'items': ITEM,
            'minItems': 1,
            'uniqueItems': True,
        },
        'order_batch': obj(
            {
                'units': {'const': 64},
                'selection': {'const': 'per_order'},
                'input_locations': {'const': 1},
            }
        ),
        'selected_profile': ID,
        'inventory_batch': obj({'input_item': ITEM, 'count': {'const': 36}}),
        'replanting_items': {
            'type': 'array',
            'items': ITEM,
            'minItems': 1,
            'uniqueItems': True,
        },
        'resource_balance_items': {
            'type': 'array',
            'items': ITEM,
            'minItems': 1,
            'uniqueItems': True,
        },
        'replanting_growth': obj(
            {'minimum_net_increase': {'const': 1}, 'single_jungle_minimum': {'const': 0}}
        ),
        'forbidden_initial_entities': {
            'type': 'array',
            'items': ITEM,
            'minItems': 1,
            'uniqueItems': True,
        },
        'spawner_conversion': obj(
            {
                'entity': {
                    'enum': [
                        'minecraft:zombie',
                        'minecraft:skeleton',
                        'minecraft:spider',
                        'minecraft:cave_spider',
                        'minecraft:breeze',
                        'minecraft:blaze',
                        'minecraft:magma_cube',
                    ]
                }
            }
        ),
        'required_dimension': DIM,
        'acceptance': obj(
            {
                'required_checks': {'type': 'array', 'items': ID, 'uniqueItems': True},
                'support': {'enum': ['supported', 'requires_extension']},
            }
        ),
        'calibration_status': {'enum': ['uncalibrated']},
        'curriculum': obj(
            {
                'stage': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                'order': {'type': 'integer', 'minimum': 1},
                'prerequisites': {'type': 'array', 'items': ID, 'uniqueItems': True},
                'focus': {'type': 'string', 'minLength': 1},
            }
        ),
        'actor_policy': obj(
            {
                'presence': {'enum': ['none', 'random_ticks', 'observer', 'player_action']},
                'allowed_drivers': {
                    'type': 'array',
                    'items': {'enum': ['none', 'attack', 'use', 'attack_and_use']},
                    'minItems': 1,
                    'uniqueItems': True,
                },
                'rationale': {'type': 'string', 'minLength': 1},
            }
        ),
    },
    [
        'id',
        'title',
        'mode',
        'primary_products',
        'required_roles',
        'mechanism_groups',
        'measurement',
        'storage',
        'allowed_driver_tools',
        'description',
        'sources',
        'suite',
        'difficulty',
        'production_kind',
        'operating_requirements',
        'product_profiles',
        'acceptance',
        'calibration_status',
        'actor_policy',
        'curriculum',
    ],
)
TASK_LIST = obj(
    {
        'protocol_version': {'const': VERSION},
        'language': {'const': 'en'},
        'difficulty': {'enum': ['easy', 'medium', 'hard', 'expert']},
        'tasks': {'type': 'array', 'items': TASK, 'minItems': 1, 'maxItems': 256},
    }
)
TASK_CATALOG = obj(
    {
        'protocol_version': {'const': VERSION},
        'language': {'const': 'en'},
        'lists': {
            'type': 'array',
            'minItems': 4,
            'maxItems': 4,
            'items': obj(
                {
                    'difficulty': {'enum': ['easy', 'medium', 'hard', 'expert']},
                    'file': {'enum': ['easy.json', 'medium.json', 'hard.json', 'expert.json']},
                    'task_count': {'type': 'integer', 'minimum': 1, 'maximum': 256},
                }
            ),
        },
    }
)
SCHEMAS = {
    'request': REQUEST,
    'response': RESPONSE,
    'action': ACTION,
    'task': TASK,
    'task-list': TASK_LIST,
    'task-catalog': TASK_CATALOG,
    'submission': SUBMISSION,
    'evaluation-report': REPORT,
    'infrastructure': INFRASTRUCTURE,
    'execution': EXECUTION,
    'server-recovery': INFRASTRUCTURE,
}


def validate(name, value):
    errors = sorted(
        Draft202012Validator(
            SCHEMAS[name], format_checker=Draft202012Validator.FORMAT_CHECKER
        ).iter_errors(value),
        key=lambda e: str(list(e.path)),
    )
    if errors:
        raise ValueError(
            '; '.join('/'.join(map(str, e.path)) + ': ' + e.message for e in errors[:12])
        )


def export():
    folder = ROOT / 'schemas'
    folder.mkdir(exist_ok=True)
    for name, schema in SCHEMAS.items():
        (folder / (name + '.schema.json')).write_text(
            json.dumps(
                {'$schema': 'https://json-schema.org/draft/2020-12/schema', **schema}, indent=2
            )
            + '\n',
            encoding='utf-8',
        )


if __name__ == '__main__':
    export()
