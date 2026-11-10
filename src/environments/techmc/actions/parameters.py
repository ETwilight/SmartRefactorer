"""Typed public parameters for formerly opaque server/local tools."""

import copy

if __package__:
    from environments.techmc.contracts import obj, POS, VEC, DIM, ID, ITEM, CONTAINER, INT32
else:
    from contracts import obj, POS, VEC, DIM, ID, ITEM, CONTAINER, INT32

WOOL_COLORS = [
    'white',
    'orange',
    'magenta',
    'light_blue',
    'yellow',
    'lime',
    'pink',
    'gray',
    'light_gray',
    'cyan',
    'purple',
    'blue',
    'brown',
    'green',
    'red',
    'black',
]
BLOCK_POSITION = {**POS, 'items': INT32}
NATIVE_CONTAINER = copy.deepcopy(CONTAINER)
NATIVE_CONTAINER['oneOf'][0]['properties']['position'] = BLOCK_POSITION


def native_action(action):
    """Translate validated public locators without altering the public request."""
    result = copy.deepcopy(action)
    if result.get('method') == 'observe.containers':
        for locator in result['params']['containers']:
            if 'position' in locator:
                locator['x'], locator['y'], locator['z'] = locator.pop('position')
    return result


def public_validation_action(action):
    """Public validation view of a native action at the internal CLI boundary.

    This does not admit xyz in the public batch-container contract. Malformed
    or ambiguous native locators remain unchanged for public validation to reject.
    """
    result = copy.deepcopy(action)
    if result.get('method') == 'observe.containers':
        for locator in result.get('params', {}).get('containers', []):
            if (
                isinstance(locator, dict)
                and all(key in locator for key in ('x', 'y', 'z'))
                and not any(key in locator for key in ('position', 'entity_uuid'))
            ):
                locator['position'] = [locator.pop(key) for key in ('x', 'y', 'z')]
    return result


def extra_schema(method):
    number = {'type': 'number'}
    integer = INT32
    text = {'type': 'string'}
    if method in ('observe.health', 'observe.performance', 'experiment.resources'):
        return obj({})
    if method in ('clock.freeze', 'clock.unfreeze', 'fake_player.list', 'trace.stop'):
        return obj({})
    if method in ('clock.step', 'clock.sprint'):
        return obj({'ticks': {'type': 'integer', 'minimum': 1, 'maximum': 2147483647}})
    if method == 'clock.rate':
        return obj({'tps': {'type': 'number', 'minimum': 1, 'maximum': 10000}})
    if method in ('counter.read', 'counter.reset'):
        return obj({'color': {'enum': WOOL_COLORS}})
    if method == 'experiment.restore':
        return obj({'keep': {'type': 'array', 'items': text}}, [])
    if method == 'experiment.wait':
        return obj(
            {
                'ticks': {'type': 'integer', 'minimum': 0},
                'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0},
                'require_natural': {'type': 'boolean'},
            },
            ['ticks'],
        )
    if method == 'experiment.measure':
        return obj(
            {
                'color': {'enum': WOOL_COLORS},
                'ticks': {'type': 'integer', 'minimum': 1, 'maximum': 2147483647},
                'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0},
                'warmup_ticks': {'type': 'integer', 'minimum': 0},
                'mode': {'enum': ['natural', 'step', 'sprint']},
            },
            ['color', 'ticks'],
        )
    if method == 'trace.start':
        return obj(
            {
                'region': {'type': 'null'},
                'categories': {
                    'type': 'array',
                    'items': text,
                    'minItems': 1,
                    'uniqueItems': True,
                },
            },
            [],
        )
    if method == 'trace.read':
        return obj(
            {
                'after_sequence': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
            },
            [],
        )
    if method == 'observe.block_entity':
        return obj(
            {'x': integer, 'y': integer, 'z': integer, 'dimension': DIM}, ['x', 'y', 'z']
        )
    if method == 'observe.container':
        common = {
            'dimension': DIM,
            'products': {'type': 'array', 'items': ITEM, 'maxItems': 32},
        }
        return {
            'oneOf': [
                obj({**common, 'x': integer, 'y': integer, 'z': integer}, ['x', 'y', 'z']),
                obj(
                    {**common, 'entity_uuid': {'type': 'string', 'format': 'uuid'}},
                    ['entity_uuid'],
                ),
            ]
        }
    if method == 'observe.containers':
        return obj(
            {
                'dimension': DIM,
                'containers': {'type': 'array', 'items': NATIVE_CONTAINER, 'maxItems': 128},
                'products': {'type': 'array', 'items': ITEM, 'maxItems': 32},
            },
            ['containers', 'products'],
        )
    if method == 'machine.control':
        common = {'x': integer, 'y': integer, 'z': integer, 'dimension': DIM}
        return {
            'oneOf': [
                obj(
                    {
                        **common,
                        'action': {'const': 'set_lever'},
                        'powered': {'type': 'boolean'},
                    },
                    ['x', 'y', 'z', 'action', 'powered'],
                ),
                obj({**common, 'action': {'const': 'press_button'}}, ['x', 'y', 'z', 'action']),
            ]
        }
    if method.startswith('fake_player.'):
        props = {'id': {'type': 'string', 'pattern': '^[A-Za-z0-9_]{1,16}$'}}
        required = ['id']
        op = method.split('.')[1]
        if op == 'spawn':
            props.update(
                x=number,
                y=number,
                z=number,
                dimension=DIM,
                yaw=number,
                pitch={'type': 'number', 'minimum': -90, 'maximum': 90},
            )
            required += ['x', 'y', 'z']
        if op in ('attack', 'use'):
            props.update(
                mode={'enum': ['once', 'continuous', 'interval']},
                interval_ticks={'type': 'integer', 'minimum': 1, 'maximum': 72000},
            )
            required += ['mode']
        if op == 'attack_and_use':
            props['mode'] = {'enum': ['continuous']}
            required += ['mode']
        if op in ('use_with_offhand', 'use_material'):
            props.update(mode={'enum': ['continuous']}, item=ITEM)
            required += ['mode', 'item']
        if op == 'move':
            props['direction'] = {'enum': ['forward', 'backward', 'left', 'right']}
            required += ['direction']
        if op == 'look':
            props.update(yaw=number, pitch={'type': 'number', 'minimum': -90, 'maximum': 90})
            required += ['yaw', 'pitch']
        schema = obj(props, required)
        if op in ('attack', 'use'):
            schema['allOf'] = [
                {
                    'if': {'properties': {'mode': {'const': 'interval'}}, 'required': ['mode']},
                    'then': {'required': ['interval_ticks']},
                }
            ]
        return schema
    return None
