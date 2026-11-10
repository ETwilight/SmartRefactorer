"""动作定义与参数校验：从元数据构造公开 schema，再校验一次动作请求。

catalog 提供动作含义和执行侧，parameters 提供专用参数类型；本模块汇合为
action_catalog 展示条目和 check_action 使用的执行前检查。
"""

import math
from jsonschema import Draft202012Validator
from .. import contracts as _contracts
from ..contracts import POS, VEC, obj, validate
from . import catalog as _action_catalog
from . import parameters as _action_parameters

TOOLS = _action_catalog.TOOLS
KNOWLEDGE_METHODS = _action_catalog.KNOWLEDGE_METHODS
LOCAL_DIAGNOSTICS = _action_catalog.LOCAL_DIAGNOSTICS
DIRECT_DIAGNOSTICS = LOCAL_DIAGNOSTICS | {'observe.performance'}
sides = _action_catalog.action_sides


def parameter_schema(method):
    extra = _action_parameters.extra_schema(method)
    if extra is not None and method not in (
        'fake_player.equip',
        'fake_player.inventory',
        'fake_player.bucket_batch',
    ):
        return extra
    integer = _action_parameters.INT32
    text = {'type': 'string'}
    boolean = {'type': 'boolean'}
    xyz = {'x': integer, 'y': integer, 'z': integer, 'dimension': {'type': 'string'}}
    if method in ('observe.blocks', 'observe.region'):
        return {
            **obj(
                {
                    'min': _action_parameters.BLOCK_POSITION,
                    'max': _action_parameters.BLOCK_POSITION,
                    'dimension': _contracts.DIM,
                },
                ['min', 'max'],
            ),
            'description': 'Inclusive min/max, min <= max on every axis; volume <= 32768 blocks. Tile larger regions into bounded calls.',
        }
    if method == 'observe.biome':
        return obj(xyz, ['x', 'y', 'z'])
    if method == 'observe.afk_site':
        return obj({'position': VEC, 'dimension': text}, ['position'])
    if method == 'command.run':
        return obj(
            {
                'text': {'type': 'string', 'minLength': 1, 'maxLength': 32767},
                'context': {
                    **obj(
                        {
                            'executor': text,
                            'dimension': _contracts.DIM,
                            'position': VEC,
                            'yaw': {'type': 'number'},
                            'pitch': {'type': 'number'},
                        },
                        [],
                    ),
                    'dependentRequired': {'pitch': ['yaw']},
                },
                'keep_conditions': boolean,
            },
            ['text'],
        )
    if method == 'command.describe':
        return {
            **obj(
                {
                    'command': {'type': 'string', 'pattern': '^[a-z_]+$'},
                    'path': {
                        'type': 'array',
                        'items': {'type': 'string', 'minLength': 1},
                        'maxItems': 32,
                    },
                    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 256},
                },
                [],
            ),
            'dependentRequired': {key: ['command'] for key in ('path', 'offset', 'limit')},
        }
    if method == 'action.move':
        return obj(
            {
                'target': VEC,
                'path': {'type': 'array', 'items': VEC, 'minItems': 1, 'maxItems': 256},
                'timeout_ms': {'type': 'integer', 'minimum': 100, 'maximum': 120000},
            },
            [],
        )
    if method == 'action.respawn':
        return obj({'timeout_ms': {'type': 'integer', 'minimum': 100, 'maximum': 120000}}, [])
    if method in ('action.interact', 'action.break'):
        return obj(
            {
                **xyz,
                'face': {
                    'enum': [
                        'UP',
                        'DOWN',
                        'NORTH',
                        'SOUTH',
                        'EAST',
                        'WEST',
                        'up',
                        'down',
                        'north',
                        'south',
                        'east',
                        'west',
                    ]
                },
                'timeout_ms': {'type': 'integer', 'minimum': 100, 'maximum': 120000},
                'hit': VEC,
                'hand': {'enum': ['main', 'off']},
            },
            ['x', 'y', 'z'],
        )
    query_text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    if method in ('wiki.search', 'source.search'):
        return obj(
            (
                {
                    'query': query_text,
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                    'language': {'enum': ['en']},
                }
                if method == 'wiki.search'
                else {
                    'query': query_text,
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                    'side': text,
                    'mode': {'enum': ['text', 'phrase', 'fts']},
                }
            ),
            ['query'],
        )
    if method == 'wiki.read':
        return obj(
            {
                'entry': text,
                'section': text,
                'fields': {
                    'type': 'array',
                    'items': {'enum': ['text', 'html', 'assets', 'links', 'headings']},
                    'minItems': 1,
                    'uniqueItems': True,
                },
            },
            ['entry'],
        )
    if method == 'wiki.provenance':
        return obj({'entry': text}, ['entry'])
    if method == 'wiki.asset':
        return obj({'path': text})
    if method == 'source.read':
        return obj(
            {
                'path': text,
                'start': {'type': 'integer', 'minimum': 1},
                'count': {'type': 'integer', 'minimum': 1, 'maximum': 500},
            },
            ['path'],
        )
    if method in (
        'observe.player',
        'observe.player_state',
        'observe.actor_state',
        'observe.actors',
        'clock.query',
        'command.describe',
        'debug.snapshot',
        'debug.capabilities',
        'debug.performance',
        'client.capture',
        'debug.capture',
        'session.describe',
        'experiment.conditions',
        'trace.stop',
    ):
        return obj({})
    if method == 'inventory.select':
        return obj({'slot': {'type': 'integer', 'minimum': 0, 'maximum': 8}})
    if method == 'fake_player.inventory':
        return obj({'id': _contracts.ID})
    if method == 'fake_player.equip':
        return obj(
            {'id': _contracts.ID, 'item': _contracts.ITEM, 'components': {'type': 'object'}}
        )
    if method == 'fake_player.bucket_batch':
        return obj(
            {
                'id': _contracts.ID,
                'product': {
                    'enum': [
                        'minecraft:lava_bucket',
                        'minecraft:powder_snow_bucket',
                        'minecraft:milk_bucket',
                    ]
                },
            }
        )
    if method == 'inventory.transfer':
        return obj(
            {
                'container_id': integer,
                'from': {'type': 'integer', 'minimum': 0},
                'to': {'type': 'integer', 'minimum': 0},
                'count': {'type': 'integer', 'minimum': 1, 'maximum': 64},
            }
        )
    return {'type': 'object'}


def action_catalog():
    return [
        {
            'method': method,
            'description': description,
            'sides': sides(method),
            'params_schema': parameter_schema(method),
            'effect': _action_catalog.action_effect(method),
            'requires_task': method not in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS,
            'category': (
                'knowledge'
                if method in KNOWLEDGE_METHODS
                else (
                    'session'
                    if method.startswith('trace.') or method in LOCAL_DIAGNOSTICS
                    else 'game'
                )
            ),
            'requires_game': method not in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS
            and not method.startswith('trace.'),
            'available_during_evaluation': method in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS,
            'execution_backends': (
                ['direct']
                if method in KNOWLEDGE_METHODS | DIRECT_DIAGNOSTICS
                else ['direct', 'cli', 'execution_agent']
            ),
        }
        for method, description in sorted(TOOLS.items())
        if sides(method) != ['client']
    ]


def check_action(action):
    validate('action', action)
    pending = [action['params']]
    while pending:
        value = pending.pop()
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Action parameters must contain only finite JSON numbers')
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if action['side'] == 'client' or sides(action['method']) == ['client']:
        raise ValueError(
            'Client interfaces are temporarily disabled; use server or local actions'
        )
    if action['method'] not in TOOLS:
        raise ValueError('Unknown primitive action')
    if action['side'] not in sides(action['method']):
        raise ValueError('Wrong execution side for ' + action['method'])
    Draft202012Validator(
        parameter_schema(action['method']), format_checker=Draft202012Validator.FORMAT_CHECKER
    ).validate(action['params'])
    if action['method'] in ('observe.blocks', 'observe.region'):
        minimum, maximum = action['params']['min'], action['params']['max']
        volume = 1
        for low, high in zip(minimum, maximum):
            if low > high:
                raise ValueError('Region min must not exceed max on any axis')
            volume *= high - low + 1
        if volume > 32768:
            raise ValueError(
                'Region read is limited to 32768 blocks; tile larger sites into bounded reads'
            )
    if action['method'] == 'action.move' and (
        ('target' in action['params']) == ('path' in action['params'])
    ):
        raise ValueError('Provide exactly one target or path')
