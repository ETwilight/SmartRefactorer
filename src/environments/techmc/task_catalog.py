"""Versioned English production tasks, grouped by difficulty."""

import copy
import hashlib
import json

if __package__:
    from .contracts import ROOT, validate
else:
    from contracts import ROOT, validate

DIFFICULTIES = ('easy', 'medium', 'hard', 'expert')
SUPPORTED_CHECKS = frozenset(
    {
        'net_output',
        'sustained_output',
        'storage_capacity',
        'chest_clearance',
        'actor_policy',
        'input_consumption',
        'bucket_batch',
        'seed_balance',
        'initial_entity_policy',
        'resource_growth',
        'single_input',
        'player_action_inventory',
        'head_weather_conditions',
        'aggregate_output',
        'forbidden_blocks',
    }
)


def available_checks(task):
    checks = set(SUPPORTED_CHECKS)
    if task.get('machine_limits'):
        checks.add('machine_bounds')
        if 'furnaces' in task['machine_limits']:
            checks.add('furnace_array')
    if task.get('variant_targets'):
        checks.update(('classified_storage', 'item_components'))
    if task.get('id') == 'medium.piglin_bartering.v1':
        checks.update(('resource_balance', 'item_components'))
    if task.get('processing_inputs'):
        checks.update(('recipe_accounting', 'fuel_autonomy'))
    if task.get('earth_inputs'):
        checks.update(
            (
                'recipe_accounting',
                'resource_balance',
                'automatic_operating_supplies',
                'operating_resources',
                'fuel_autonomy',
            )
        )
    if (
        task.get('mode_trials')
        or task.get('brew_orders')
        or task.get('order_batch')
        or task.get('mode_requirements', {}).get('measurement_per_mode')
    ):
        checks.update(
            ('machine_modes', 'configurable_batches', 'brew_orders', 'item_components')
        )
    checks.update(
        set(task.get('mandatory_review_checks', []))
        & {'integrated_head_disc_machine', 'shared_tree_machine', 'shared_processing'}
    )
    if task.get('material_conversion'):
        checks.add('recipe_accounting')
    if task.get('material_growth'):
        checks.update(('resource_balance', 'source_replenishment', 'material_net_growth'))
    if task.get('natural_source'):
        checks.add('natural_source_provenance')
    if task.get('natural_source') == 'amethyst_geode':
        checks.add('protected_sources')
    if task.get('id') in (
        'hard.azalea_leaves.v1',
        'hard.amethyst.v1',
        'expert.universal_tree_farm.v1',
    ):
        checks.add('integrated_operation')
    if task.get('id') == 'hard.nether_stars.v1':
        checks.add('recipe_accounting')
    if task.get('id') in ('hard.raid.v1', 'hard.trial_products.v1'):
        checks.update(
            (
                'combined_player_operation',
                'raid_operator',
                'consumable_accounting',
                'item_components',
            )
        )
    return checks


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()


def list_tasks(difficulty=None, world_type=None):
    if world_type is not None and world_type not in ('void', 'natural'):
        raise ValueError('Unknown world type: ' + str(world_type))
    if difficulty is not None and difficulty not in DIFFICULTIES:
        raise ValueError('Unknown difficulty: ' + str(difficulty))
    manifest = json.loads((ROOT / 'tasks/catalog.json').read_text(encoding='utf-8'))
    validate('task-catalog', manifest)
    values = []
    if [entry['difficulty'] for entry in manifest['lists']] != list(DIFFICULTIES):
        raise ValueError('Expected easy, medium, hard and expert task lists')
    for entry in manifest['lists']:
        if entry['file'] != entry['difficulty'] + '.json':
            raise ValueError('Unexpected task list path')
        group = json.loads((ROOT / 'tasks' / entry['file']).read_text(encoding='utf-8'))
        validate('task-list', group)
        if entry['task_count'] != len(group['tasks']):
            raise ValueError('Task count does not match the catalogue index')
        if group['difficulty'] != entry['difficulty'] or not 1 <= len(group['tasks']) <= 256:
            raise ValueError('Task list size is outside the supported range')
        for task in group['tasks']:
            validate('task', task)
            if task['difficulty'] != entry['difficulty']:
                raise ValueError('Task difficulty does not match its list')
            if not json.dumps(task, ensure_ascii=False).isascii():
                raise ValueError('Published task text must be English')
            m = task['measurement']
            segments = m.get('segments', 1)
            if m['window_ticks'] % segments:
                raise ValueError('Measurement window must divide into equal segments')
            if m['mode'] not in m['allowed_modes']:
                raise ValueError('Default tick mode must be allowed by the task')
            if task['mode'] == 'threshold' and (
                not task['primary_products']
                or m['min_net_items'] is None
                or m['min_per_game_hour'] is None
            ):
                raise ValueError('Threshold task must fix products and numeric goals')
            if task['mode'] == 'threshold' and (
                m['min_net_items'] <= 0 or m['min_per_game_hour'] <= 0
            ):
                raise ValueError('Threshold goals must be positive')
            if task['mode'] == 'open_efficiency' and (
                m['min_net_items'] is not None or m['min_per_game_hour'] is not None
            ):
                raise ValueError('Open task must not invent a numeric pass threshold')
            policy = task['actor_policy']
            if task.get('spawner_conversion') and 'spawner' not in task['title'].lower():
                raise ValueError(
                    'Spawner scenarios must explicitly identify spawner conversion in the task title'
                )
            if 'inventory_batch' in task:
                if task['inventory_batch'] != {
                    'input_item': 'minecraft:bucket',
                    'count': 36,
                } or task['primary_products'] not in (
                    ['minecraft:lava_bucket'],
                    ['minecraft:powder_snow_bucket'],
                    ['minecraft:milk_bucket'],
                ):
                    raise ValueError('Unsupported inventory batch')
                if (
                    policy['presence'] != 'player_action'
                    or m['min_net_items'] != 36
                    or m.get('segments') != 1
                ):
                    raise ValueError(
                        'Bucket batch requires a player, 36 items and one batch window'
                    )
                if task['storage'] != {
                    'destination': 'evaluator_inventory',
                    'reserved_stack_slots_per_primary': 36,
                    'headroom_ticks': 0,
                    'min_free_stack_slots': 0,
                }:
                    raise ValueError(
                        'Bucket batch uses exactly 36 inventory slots without spare capacity'
                    )
            elif (
                task['storage']['headroom_ticks'] < 1
                or task['storage']['min_free_stack_slots'] < 1
            ):
                raise ValueError('Continuous output requires storage headroom')
            if 'seed_balance' in task['acceptance']['required_checks'] and not task.get(
                'replanting_items'
            ):
                raise ValueError('Seed balance requires declared replanting items')
            if policy['presence'] == 'none' and policy['allowed_drivers'] != ['none']:
                raise ValueError('Action drivers require a player')
            if (
                any(d != 'none' for d in policy['allowed_drivers'])
                and policy['presence'] != 'player_action'
            ):
                raise ValueError('Action drivers require the player_action policy')
            if policy['allowed_drivers'] == ['none'] and task['allowed_driver_tools']:
                raise ValueError('Idle observers must not receive driver tools')
            profiles = task['product_profiles']
            published_products = {p for profile in profiles for p in profile['products']}
            if set(m.get('product_targets', {})) - published_products:
                raise ValueError('Product targets must name published products')
            targets = m.get('aggregate_targets', [])
            if len({g['id'] for g in targets}) != len(targets):
                raise ValueError('Duplicate aggregate target IDs')
            if any(set(g['products']) - published_products for g in targets):
                raise ValueError('Aggregate targets must name published products')
            if 'aggregate_output' in task['acceptance']['required_checks'] and not targets:
                raise ValueError('Aggregate output requires configured targets')
            if 'forbidden_blocks' in task['acceptance']['required_checks'] and not task.get(
                'forbidden_blocks'
            ):
                raise ValueError('Forbidden blocks require configuration')
            if len({p['id'] for p in profiles}) != len(profiles):
                raise ValueError('Duplicate product profile IDs')
            if task['primary_products'] != profiles[0]['products']:
                raise ValueError('Default products must match the first profile')
            expected = (
                'requires_extension'
                if set(task['acceptance']['required_checks']) - available_checks(task)
                else 'supported'
            )
            for check, field in (
                ('resource_growth', 'resource_balance_items'),
                ('initial_entity_policy', 'forbidden_initial_entities'),
            ):
                if check in task['acceptance']['required_checks'] and not task.get(field):
                    raise ValueError('Missing configuration for ' + check)
            if (
                expected == 'supported'
                and 'single_input' in task['acceptance']['required_checks']
                and not task.get('single_input_items')
            ):
                raise ValueError('Missing single-input feedstock configuration')
            if task['acceptance']['support'] != expected:
                raise ValueError('Task evaluator support metadata is stale')
        values.extend(group['tasks'])
    if len({t['id'] for t in values}) != len(values):
        raise ValueError('Duplicate task IDs')
    by_id = {t['id']: t for t in values}
    for task in values:
        for prerequisite in task['curriculum']['prerequisites']:
            if (
                prerequisite not in by_id
                or by_id[prerequisite]['curriculum']['order'] >= task['curriculum']['order']
            ):
                raise ValueError('Curriculum prerequisites must name earlier tasks')
    return [
        {**copy.deepcopy(t), 'task_hash': stable_hash(t)}
        for t in values
        if (difficulty is None or t['difficulty'] == difficulty)
        and (world_type is None or t.get('world_type', 'void') == world_type)
    ]


def get_task(task_id):
    return next((task for task in list_tasks() if task['id'] == task_id), None) or _unknown(
        task_id
    )


def select_profile(task, profile_id=None):
    task = copy.deepcopy(task)
    profiles = task['product_profiles']
    selected = next((p for p in profiles if p['id'] == (profile_id or profiles[0]['id'])), None)
    if selected is None:
        raise ValueError('Unknown product profile: ' + str(profile_id))
    task.pop('task_hash', None)
    task['selected_profile'] = selected['id']
    task['primary_products'] = selected['products']
    if selected.get('earth_inputs'):
        task['earth_inputs'] = copy.deepcopy(selected['earth_inputs'])
        task['single_input_items'] = copy.deepcopy(selected['earth_inputs'])
    if selected.get('required_recipe_observers'):
        task['required_recipe_observers'] = copy.deepcopy(selected['required_recipe_observers'])
    if task.get('id') == 'hard.granular_materials.v1':
        task['material_growth'] = copy.deepcopy(selected['products'])
    if task.get('material_conversion'):
        task['material_conversion'] = {
            'input': selected['products'][0] + '_powder',
            'output': selected['products'][0],
            'input_units': 1,
            'output_units': 1,
        }
        task['single_input_items'] = [task['material_conversion']['input']]
    if selected.get('processing_inputs'):
        task['processing_inputs'] = copy.deepcopy(selected['processing_inputs'])
        task['single_input_items'] = copy.deepcopy(selected['processing_inputs'])
    elif task.get('processing_inputs'):
        task.pop('processing_inputs')
        task.pop('single_input_items', None)
    if 'product_targets' in task['measurement']:
        task['measurement']['product_targets'] = {
            p: v
            for p, v in task['measurement']['product_targets'].items()
            if p in selected['products']
        }
    if 'aggregate_targets' in task['measurement']:
        task['measurement']['aggregate_targets'] = [
            {**g, 'products': [p for p in g['products'] if p in selected['products']]}
            for g in task['measurement']['aggregate_targets']
            if set(g['products']) & set(selected['products'])
        ]
    task['acceptance']['required_checks'] = list(
        dict.fromkeys(
            task['acceptance']['required_checks'] + selected.get('required_checks', [])
        )
    )
    if selected.get('inventory_batch'):
        task['inventory_batch'] = copy.deepcopy(selected['inventory_batch'])
        task['measurement'].update(
            min_net_items=36,
            min_per_game_hour=36 * 72000 / task['measurement']['window_ticks'],
            segments=1,
        )
        task['storage'] = {
            'destination': 'evaluator_inventory',
            'reserved_stack_slots_per_primary': 36,
            'headroom_ticks': 0,
            'min_free_stack_slots': 0,
        }
        task['acceptance']['required_checks'] = ['bucket_batch', 'actor_policy']
        task['description'] = (
            'Fill all 36 supplied empty buckets with the selected product within the time limit. Keep all filled buckets in the evaluator inventory; no replenishment.'
        )
        task['operating_requirements'] = [
            'The evaluator supplies one empty bucket in each of the 36 main inventory and hotbar slots.',
            'Fill every supplied bucket within the time limit; no replenishment or external output storage.',
        ]
    task['acceptance']['support'] = (
        'requires_extension'
        if set(task['acceptance']['required_checks']) - available_checks(task)
        else 'supported'
    )
    task['task_hash'] = stable_hash(task)
    return task


def _unknown(task_id):
    raise ValueError('Unknown task ID: ' + str(task_id))
