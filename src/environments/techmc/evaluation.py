"""机器验收主流程：校验提交、采集测量、计算规则并写入结果。

一次验收将任务版本、提交位置、原生观测和测量窗口绑定在同一份记录中。
acceptance/ 按机器类型提供物料守恒、产量和结构检查。
"""

import copy
import json
import math
import re
import time
import uuid
from pathlib import Path

if __package__:
    from .failure_contract import report as canonical_report, fault, write_measurement
    from .contracts import validate, VERSION
    from .task_catalog import stable_hash, SUPPORTED_CHECKS, available_checks
    from .runtime_support import ROOT, atomic_json, natural_wait
    from .adapters.bridge import BridgeError, NativeExecutionError, native_execution_state
    from .evaluator_actor import EvaluatorActor
    from environments.techmc.acceptance.modes import trials_for
    from environments.techmc.acceptance.modes import required_review
    from environments.techmc.acceptance.modes import validate_mode_submission
    from environments.techmc.acceptance.modes import validate_review_volumes
    from environments.techmc.acceptance.modes import run_modes
    from environments.techmc.acceptance.modes import apply_controls
    from environments.techmc.acceptance.modes import count_components
    from environments.techmc.acceptance.modes import exact_delivery
    from environments.techmc.acceptance.earth import earth_mass_balance
    from environments.techmc.acceptance.earth import earth_new_output
    from environments.techmc.acceptance.earth import earth_operating_supplies
    from environments.techmc.acceptance.furnace import validate_machine_limits
    from environments.techmc.acceptance.furnace import furnace_activity
    from environments.techmc.acceptance.harvest import collect_harvest
    from environments.techmc.acceptance.harvest import amethyst_ineligible_drops
    from environments.techmc.acceptance.wither import collect_wither_sources
    from environments.techmc.acceptance.wither import natural_wither_output
    from environments.techmc.acceptance.components import classified_counts
    from environments.techmc.acceptance.components import variant_stock
    from environments.techmc.acceptance.components import validate_barter_components
    from environments.techmc.acceptance.components import barter_balance
    from environments.techmc.acceptance.processing import cold_start
    from environments.techmc.acceptance.processing import cooking_balance
    from environments.techmc.acceptance.processing import autonomous_fuel
    from environments.techmc.acceptance.processing import conversion_balance
    from environments.techmc.acceptance.processing import wither_recipe_balance
    from environments.techmc.acceptance.processing import material_growth_balance
    from environments.techmc.acceptance.processing import world_output_balance
else:
    from failure_contract import report as canonical_report, fault, write_measurement
    from contracts import validate, VERSION
    from task_catalog import stable_hash, SUPPORTED_CHECKS, available_checks
    from runtime_support import ROOT, atomic_json, natural_wait
    from adapters.bridge import BridgeError, NativeExecutionError, native_execution_state
    from evaluator_actor import EvaluatorActor
    from acceptance.modes import (
        trials_for,
        required_review,
        validate_mode_submission,
        validate_review_volumes,
        run_modes,
        apply_controls,
        count_components,
        exact_delivery,
    )
    from acceptance.earth import earth_mass_balance, earth_new_output, earth_operating_supplies
    from acceptance.furnace import validate_machine_limits, furnace_activity
    from acceptance.harvest import collect_harvest, amethyst_ineligible_drops
    from acceptance.wither import collect_wither_sources, natural_wither_output
    from acceptance.components import (
        classified_counts,
        variant_stock,
        validate_barter_components,
        barter_balance,
    )
    from acceptance.processing import (
        cold_start,
        cooking_balance,
        autonomous_fuel,
        conversion_balance,
        wither_recipe_balance,
        material_growth_balance,
        world_output_balance,
    )


def _bound_native_error(exc, step):
    frame = getattr(exc, 'native_feedback', None)
    request = getattr(exc, 'native_request', None)
    if not isinstance(frame, dict) or not isinstance(request, dict):
        return None
    if not all(request.get(k) for k in ('run_id', 'actor_id', 'request_id', 'method')):
        return None
    if (
        request.get('request_id') != step['request_id']
        or request.get('method') != step['method']
        or request.get('params') != step.get('params', {})
    ):
        return None
    if frame.get('source') != step.get('side', 'server') or any(
        frame.get(k) != request.get(k) for k in ('run_id', 'actor_id', 'request_id', 'method')
    ):
        return None
    return frame


def _forceload_no_change(exc, step, max_chunks):
    """Only an exact terminal no-add callback is eligible for a read-only proof."""
    frame = _bound_native_error(exc, step)
    if (
        not isinstance(exc, NativeExecutionError)
        or exc.execution_state != 'completed'
        or not frame
        or frame.get('status') != 'ok'
    ):
        return None
    params = step.get('params', {})
    text = params.get('text', '')
    match = re.fullmatch(r'forceload add (-?\d+) (-?\d+) (-?\d+) (-?\d+)', text)
    if step['method'] != 'command.run' or step.get('side', 'server') != 'server' or not match:
        return None
    if (
        set(params) != {'text', 'context'}
        or not isinstance(params['context'], dict)
        or set(params['context']) != {'dimension'}
    ):
        return None
    body = frame.get('result', {})
    callbacks = body.get('callbacks')
    if (
        body.get('command') != text
        or body.get('message_keys') != ['commands.forceload.added.failure']
        or not isinstance(callbacks, list)
        or len(callbacks) != 1
        or callbacks[0].get('success') is not False
        or type(callbacks[0].get('value')) is not int
        or callbacks[0]['value'] != 0
    ):
        return None
    x1, z1, x2, z2 = map(int, match.groups())
    if x1 > x2 or z1 > z2:
        return None
    if (x2 // 16 - x1 // 16 + 1) * (z2 // 16 - z1 // 16 + 1) > max_chunks:
        return None
    return [
        (x * 16, z * 16)
        for x in range(x1 // 16, x2 // 16 + 1)
        for z in range(z1 // 16, z2 // 16 + 1)
    ]


def invoke_measurement(session, step, observations, *, evaluation_deadline, max_chunks=64):
    """Preserve terminal native errors; only pending requests reuse their ID.

    A forceload add that changes nothing is not silently declared successful:
    exact read-only per-chunk query evidence must establish its postcondition.
    The original failed callback remains intact in the acceptance evidence.
    """
    deadline = time.monotonic() + 120

    def retain(frame, error=None):
        item = {
            'id': 'obs-' + str(len(observations)),
            'method': step['method'],
            'request': copy.deepcopy(step),
            'params': copy.deepcopy(step.get('params', {})),
            'result': copy.deepcopy(frame.get('result', frame)),
        }
        if error is not None:
            item.update(
                native_feedback=copy.deepcopy(frame),
                native_request=copy.deepcopy(error.native_request),
                error=fault(error),
                execution_state=error.execution_state,
            )
        observations.append(item)
        return item

    while True:
        if time.monotonic() > evaluation_deadline:
            raise RuntimeError('Infrastructure: acceptance wall-time resource limit exceeded')
        try:
            result = session.invoke(step)
        except BridgeError as exc:
            frame = _bound_native_error(exc, step)
            if frame is not None:
                pending = frame.get('status') == 'pending_or_unknown'
            elif getattr(exc, 'native_feedback', None) is not None:
                raise  # mismatched typed evidence never becomes a poll or success
            elif isinstance(exc, NativeExecutionError):
                raise
            else:
                try:
                    legacy = json.loads(str(exc))
                except (ValueError, TypeError):
                    raise RuntimeError('Malformed native RPC error: ' + str(exc)) from exc
                pending = (
                    isinstance(legacy, dict) and legacy.get('status') == 'pending_or_unknown'
                )
            if pending and time.monotonic() <= deadline:
                if frame is not None:
                    retain(frame, exc)
                time.sleep(0.5)
                continue
            if frame is not None:
                original = retain(frame, exc)
                chunks = _forceload_no_change(exc, step, max_chunks)
                if chunks is not None:
                    queries = []
                    for x, z in chunks:
                        params = {
                            'text': f'forceload query {x} {z}',
                            'context': copy.deepcopy(step['params']['context']),
                        }
                        query = {**step, 'params': params, 'request_id': str(uuid.uuid4())}
                        answer = invoke_measurement(
                            session,
                            query,
                            observations,
                            evaluation_deadline=evaluation_deadline,
                            max_chunks=max_chunks,
                        )
                        request = {
                            'method': 'command.run',
                            'params': params,
                            'request_id': query['request_id'],
                            'run_id': session.run_id,
                            'actor_id': session.actor,
                        }
                        payload = answer.get('result', {})
                        callbacks = payload.get('callbacks')
                        if (
                            native_execution_state(answer, request, 'server') != 'completed'
                            or answer.get('status') != 'ok'
                            or payload.get('command') != params['text']
                            or payload.get('message_keys')
                            != ['commands.forceload.query.success']
                            or not isinstance(callbacks, list)
                            or len(callbacks) != 1
                            or callbacks[0].get('success') is not True
                            or type(callbacks[0].get('value')) is not int
                            or callbacks[0]['value'] != 1
                        ):
                            raise exc
                        observations[-1]['native_feedback'] = copy.deepcopy(answer)
                        queries.append(observations[-1]['id'])
                    original['postcondition_verification'] = {
                        'kind': 'all_requested_chunks_forced',
                        'satisfied': True,
                        'evidence': queries,
                        'native_callback_unchanged': True,
                    }
                    return frame
            raise
        retain(result)
        return result


def bounds_volume(bounds):
    sizes = [b - a + 1 for a, b in zip(bounds['min'], bounds['max'])]
    if min(sizes) <= 0:
        raise ValueError('Bounds must be ordered')
    return math.prod(sizes)


def contains(bounds, position):
    return all(a <= n <= b for a, b, n in zip(bounds['min'], bounds['max'], position))


def submission_regions(submission):
    return [
        {'dimension': submission['dimension'], 'bounds': submission['bounds']}
    ] + submission.get('regions', [])


def in_regions(submission, position, dimension):
    return any(
        r['dimension'] == dimension and contains(r['bounds'], position)
        for r in submission_regions(submission)
    )


def maintained_thunderstorm(conditions):
    world = next(
        (
            w
            for w in conditions.get('worlds', [])
            if w.get('dimension') == 'minecraft:overworld'
        ),
        {},
    )
    return (
        world.get('raining') is True
        and world.get('thundering') is True
        and world.get('gamerules', {}).get('minecraft:advance_weather') is False
    )


def validate_submission(task, instance_id, submission, max_volume=262144):
    validate('submission', submission)
    validate_mode_submission(task, submission)
    validate_review_volumes(task, submission)
    validate_machine_limits(task, submission)
    if submission['task_instance_id'] != instance_id:
        raise ValueError('Submission belongs to another task instance')
    if task.get('required_dimension') and submission['dimension'] != task['required_dimension']:
        raise ValueError('Task requires dimension ' + task['required_dimension'])
    bounds = submission['bounds']
    regions = submission_regions(submission)
    volume = sum(bounds_volume(r['bounds']) for r in regions)
    if volume > max_volume:
        raise ValueError(
            'Inspection exceeds configured resource budget; administrator may raise the budget'
        )
    for i, left in enumerate(regions):
        for right in regions[i + 1 :]:
            if left['dimension'] == right['dimension'] and all(
                a <= d and c <= b
                for a, b, c, d in zip(
                    left['bounds']['min'],
                    left['bounds']['max'],
                    right['bounds']['min'],
                    right['bounds']['max'],
                )
            ):
                raise ValueError('Declared regions must not overlap in the same dimension')
    roles = {p['role'] for p in submission['inspection_points']}
    missing = set(task['required_roles']) - roles
    if missing:
        raise ValueError('Missing required inspection roles: ' + ', '.join(sorted(missing)))
    products = [o['product'] for o in submission['outputs']]
    if len(set(products)) != len(products):
        raise ValueError('Each primary product must occur exactly once')
    if task['primary_products'] is not None and set(products) != set(task['primary_products']):
        raise ValueError(
            'Submission must include every task primary product, without replacing the target'
        )
    if (
        'single_input' in task.get('acceptance', {}).get('required_checks', [])
        and len(submission['inputs']) > 1
    ):
        raise ValueError('Preloaded feedstock must use one input location')
    batch = task.get('inventory_batch')
    if not batch and any(not out['containers'] for out in submission['outputs']):
        raise ValueError('Each output requires declared storage')
    if batch and (
        any(out['containers'] for out in submission['outputs'])
        or submission['operation']['driver'] != 'use'
        or submission['operation']['tool'] != 'minecraft:bucket'
    ):
        raise ValueError(
            'Bucket batch outputs stay in the evaluator inventory and require the use driver'
        )
    refs = submission['inputs'] + [r for o in submission['outputs'] for r in o['containers']]
    if task.get('variant_targets'):
        variants = submission.get('variant_outputs', {})
        if set(variants) != {t['id'] for t in task['variant_targets']}:
            raise ValueError('Declare storage for every required item variant')
        if {stable_hash(r) for v in variants.values() for r in v} != {
            stable_hash(r) for o in submission['outputs'] for r in o['containers']
        }:
            raise ValueError(
                'Variant storage must cover exactly the declared output containers'
            )
        refs += [r for v in variants.values() for r in v]
    points = [
        (p['position'], p.get('dimension', submission['dimension']))
        for p in submission['inspection_points']
    ]
    points += [
        (r['position'], r.get('dimension', submission['dimension']))
        for r in refs
        if 'position' in r
    ]
    if any(not in_regions(submission, p, d) for p, d in points):
        raise ValueError(
            'Inspection points, block containers and collection paths must be inside declared bounds'
        )
    for output in submission['outputs']:
        dimensions = {
            r.get('dimension', submission['dimension']) for r in output['containers']
        } or {submission['dimension']}
        if any(
            not any(in_regions(submission, p, d) for d in dimensions)
            for p in output['collection_path']
        ):
            raise ValueError('Collection paths must be inside declared output regions')
    operation = submission['operation']
    if operation['driver'] != 'none' and not in_regions(
        submission, operation['afk_position'], submission['dimension']
    ):
        raise ValueError(
            'Active operating players must remain inside the declared machine regions so their material stock remains in the system'
        )
    if task['id'] == 'hard.raid.v1' and operation['driver'] != 'attack_and_use':
        raise ValueError(
            'Raid production requires the combined attack and bottle-use operation'
        )
    if (
        task.get('selected_profile') == 'ominous_keys'
        and operation['driver'] != 'attack_and_use'
    ):
        raise ValueError(
            'Ominous trial keys require the combined attack and bottle-use operation'
        )
    if operation.get('offhand_item'):
        if operation['driver'] != 'use' or operation['offhand_item'] not in task.get(
            'allowed_offhand_items', []
        ):
            raise ValueError('Offhand material is not permitted for this operation')
        if not any(c['item'] == operation['offhand_item'] for c in operation['consumables']):
            raise ValueError('Offhand material requires a declared consumable source')
    if operation.get('tool_components') and operation['tool'] is None:
        raise ValueError('Tool components require an operating tool')
    if operation.get('tool_components') and operation['tool'] in task.get(
        'material_driver_items', []
    ):
        raise ValueError(
            'Consumable driver materials must be supplied by the machine, without evaluator-injected components'
        )
    if (
        operation['tool'] in task.get('material_driver_items', [])
        and operation['driver'] != 'use'
    ):
        raise ValueError('Consumable driver materials require the declared use operation')
    policy = task.get('actor_policy')
    if policy and operation['driver'] not in policy['allowed_drivers']:
        raise ValueError(
            'Task actor policy forbids this driver; automate harvesting in the machine'
        )
    if operation['driver'] != 'none':
        if operation['target'] is None or not contains(bounds, operation['target']):
            raise ValueError('Driver needs an in-bounds target')
        if operation['aim_point'] is None or any(
            not a <= v <= a + 1 for a, v in zip(operation['target'], operation['aim_point'])
        ):
            raise ValueError('Driver needs an explicit aim point within its target block')
        if operation['tool'] not in task['allowed_driver_tools']:
            raise ValueError('Driver tool is not permitted by this task')
    elif (
        operation['target'] is not None
        or operation['tool'] is not None
        or operation['aim_point'] is not None
    ):
        raise ValueError('No-driver operation must use null target/tool/aim_point')
    for point in submission['inspection_points']:
        if (
            point.get('dimension', submission['dimension']) == submission['dimension']
            and (not policy or policy['presence'] != 'none')
            and point['role'] in ('crop', 'production')
            and math.dist(point['position'], operation['afk_position']) > 128
        ):
            raise ValueError(
                'Declared AFK position is outside the production random-tick radius'
            )
    declared_inputs = {stable_hash(r) for r in submission['inputs']}
    if any(stable_hash(c['source']) not in declared_inputs for c in operation['consumables']):
        raise ValueError('Every consumable source must also be declared in inputs')
    return {
        'valid': True,
        'products': products,
        'volume': volume,
        'required_roles': task['required_roles'],
    }


def tiles(bounds):
    # Reading batches is a resource limit, not a prescribed machine dimension.
    for x in range(bounds['min'][0], bounds['max'][0] + 1, 16):
        for y in range(bounds['min'][1], bounds['max'][1] + 1, 64):
            for z in range(bounds['min'][2], bounds['max'][2] + 1, 16):
                yield {
                    'min': [x, y, z],
                    'max': [
                        min(x + 15, bounds['max'][0]),
                        min(y + 63, bounds['max'][1]),
                        min(z + 15, bounds['max'][2]),
                    ],
                }


def locator(ref, dimension):
    result = {'dimension': ref.get('dimension', dimension)}
    if 'position' in ref:
        result.update(zip(('x', 'y', 'z'), ref['position']))
    else:
        result['entity_uuid'] = ref['entity_uuid']
    return result


def storage_view(submission, snapshots, boxed=False):
    all_slots = {}
    eligible = {}
    counts = {}
    for output in submission['outputs']:
        product = output['product']
        eligible[product] = set()
        counts[product] = 0
        for ref in output['containers']:
            snapshot = snapshots[stable_hash(ref)]
            if not snapshot.get('loaded'):
                raise ValueError('Declared output container is unloaded')
            if (
                ref.get('dimension', submission['dimension']) != submission['dimension']
                and snapshot.get('dimension') != ref['dimension']
            ):
                raise RuntimeError(
                    'Container dimension telemetry unavailable or mismatched; update the server bridge'
                )
            if any(
                not in_regions(submission, p, ref.get('dimension', submission['dimension']))
                for p in snapshot.get('locations', [])
            ):
                raise ValueError(
                    'All physical container members must fit inside the declared bounds'
                )
            if boxed and snapshot.get('inventory_accounting_version') != 'nested-items-v1':
                raise RuntimeError(
                    'Shulker inventory telemetry unavailable; update the server bridge'
                )
            storage_slots = (
                []
                if boxed
                else snapshot['slots']
                + [
                    child
                    for slot in snapshot['slots']
                    for child in slot.get('shulker_slots', [])
                ]
            )
            if boxed:
                for outer in snapshot['slots']:
                    storage_slots.extend(outer.get('shulker_slots', []))
                    if outer['count'] == 0 and outer.get('accepts_shulker'):
                        # Reserve destination space for boxes the machine will pack.
                        # Empty shells and virtual slots contribute no product credit.
                        for index in range(27):
                            storage_slots.append(
                                {
                                    'slot_id': outer['slot_id'] + '/future_box/' + str(index),
                                    'item': 'minecraft:air',
                                    'count': 0,
                                    'products': {
                                        p: {
                                            'max_stack': v['max_stack'],
                                            'capacity': v['max_stack'],
                                            'free': v['max_stack'],
                                        }
                                        for p, v in outer['products'].items()
                                    },
                                }
                            )
            for slot in storage_slots:
                key = slot['slot_id']
                all_slots[key] = slot
                eligible[product].add(key)
        counts[product] = sum(
            all_slots[k]['count'] for k in eligible[product] if all_slots[k]['item'] == product
        )
    return all_slots, eligible, counts


def item_stock(region, item):
    """Total contents across inventory, packed boxes and loose items, excluding players."""
    if 'total_item_counts' in region:
        return region['total_item_counts'].get(item, 0)
    return region['inventory_counts'].get(item, 0) + region['loose_item_counts'].get(item, 0)


def allocate_storage(slots, eligible, required=54, headroom=None):
    # Bipartite slot allocation prevents counting the same empty slot once for
    # every product. Physical slot IDs also deduplicate both double-chest halves.
    candidates = {
        p: sorted(
            (
                k
                for k in keys
                if slots[k]['products'][p]['capacity'] >= slots[k]['products'][p]['max_stack']
            ),
            key=lambda k: slots[k]['products'][p]['free'],
            reverse=True,
        )
        for p, keys in eligible.items()
    }
    owners = {}
    assignments = {}

    def assign(unit, visited):
        product = unit[0]
        for key in candidates[product]:
            if key in visited:
                continue
            visited.add(key)
            if key not in owners or assign(owners[key], visited):
                owners[key] = unit
                assignments[unit] = key
                return True
        return False

    for product in sorted(candidates, key=lambda p: len(candidates[p])):
        for i in range(required):
            if not assign((product, i), set()):
                blockers = [
                    {
                        'slot_id': key,
                        'item': slots[key]['item'],
                        'count': slots[key]['count'],
                        **slots[key]['products'][product],
                    }
                    for key in eligible[product]
                    if key not in candidates[product]
                ]
                return {
                    'passed': False,
                    'reason': 'Insufficient distinct compatible storage slots',
                    'missing_product': product,
                    'required_slots_per_product': required,
                    'available_compatible_slots': len(candidates[product]),
                    'distinct_physical_slots': len(eligible[product]),
                    'compatible_slots_by_product': {p: len(v) for p, v in candidates.items()},
                    'blocker_count': len(blockers),
                    'blockers': sorted(blockers, key=lambda s: s['slot_id'])[:16],
                    'blockers_truncated': len(blockers) > 16,
                    'shared_allocation_conflict': len(candidates[product]) >= required,
                }
    # The minimum reservation is not an upper bound. Allocate additional slots
    # for high-throughput buffers instead of ignoring the rest of the warehouse.
    if headroom:
        units = {p: required for p in candidates}
        while True:
            free = {
                p: sum(
                    slots[key]['products'][p]['free']
                    for unit, key in assignments.items()
                    if unit[0] == p
                )
                for p in candidates
            }
            deficits = sorted(
                (p for p in candidates if free[p] < headroom.get(p, 0)),
                key=lambda p: headroom[p] - free[p],
                reverse=True,
            )
            if not deficits:
                break
            advanced = False
            for product in deficits:
                if assign((product, units[product]), set()):
                    units[product] += 1
                    advanced = True
                    break
            if not advanced:
                break
    result = {}
    for product in candidates:
        keys = [key for unit, key in assignments.items() if unit[0] == product]
        result[product] = {
            'reserved_slots': len(keys),
            'slot_ids': keys,
            'capacity_items': sum(slots[k]['products'][product]['capacity'] for k in keys),
            'free_items': sum(slots[k]['products'][product]['free'] for k in keys),
            'max_stack': slots[keys[0]]['products'][product]['max_stack'],
        }
    return {'passed': True, 'products': result, 'required_slots_per_product': required}


def output_access(submission, snapshots):
    """Chest obstruction is checked by vanilla code, not block-name heuristics.

    This proves lid clearance only, not a walkable path, unlock permission or
    successful GUI interaction. Other container types retain capacity semantics.
    """
    for output in submission['outputs']:
        for ref in output['containers']:
            value = snapshots[stable_hash(ref)]
            if value.get('type') in ('minecraft:chest', 'minecraft:trapped_chest'):
                if value.get('openable') is None:
                    raise RuntimeError(
                        'Chest accessibility readback unavailable; rebuild/restart the server bridge'
                    )
                if not value['openable']:
                    return False
    return True


def production_metrics(task, start, end, initial_inflight, scope='total'):
    ticks = end['tick'] - start['tick']
    seconds = (end['sample_monotonic_ns'] - start['sample_monotonic_ns']) / 1e9
    if ticks <= 0 or seconds <= 0:
        raise ValueError(
            'Measurement needs positive actual game ticks and server monotonic duration'
        )
    result = {}
    for product, before in start['counts'].items():
        gross = end['counts'][product] - before
        if gross < 0:
            raise RuntimeError(
                'Output inventory decreased during the exclusive acceptance window'
            )
        net = max(0, gross - initial_inflight.get(product, 0))
        target = (
            task['measurement'].get('product_targets', {}).get(product, task['measurement'])
        )
        rate_ok = (
            None
            if task['mode'] == 'open_efficiency'
            else net * 72000 / ticks >= target['min_per_game_hour']
        )
        net_ok = (
            None
            if scope == 'segment' or task['mode'] == 'open_efficiency'
            else net >= target['min_net_items']
        )
        result[product] = {
            'gross_collected_delta': gross,
            'initial_inflight_excluded': initial_inflight.get(product, 0),
            'net_new_collected': net,
            'elapsed_game_ticks': ticks,
            'elapsed_wall_seconds': seconds,
            'per_game_hour': net * 72000 / ticks,
            'per_wall_hour': net * 3600 / seconds,
            'threshold_scope': scope,
            'required_min_net_items': None if scope == 'segment' else target['min_net_items'],
            'required_min_per_game_hour': target['min_per_game_hour'],
            'rate_threshold_passed': rate_ok,
            'net_threshold_passed': net_ok,
            'threshold_passed': (
                None
                if task['mode'] == 'open_efficiency'
                else rate_ok and (net > 0 if scope == 'segment' else net_ok)
            ),
        }
    return result


def aggregate_output_metrics(task, metrics, scope='total'):
    result = {}
    for group in task['measurement'].get('aggregate_targets', []):
        members = [metrics[p] for p in group['products'] if p in metrics]
        if not members:
            continue
        target = group['target']
        net = sum(p['net_new_collected'] for p in members)
        rate = sum(p['per_game_hour'] for p in members)
        result[group['id']] = {
            'products': [p for p in group['products'] if p in metrics],
            'net_new_collected': net,
            'per_game_hour': rate,
            'required_min_per_game_hour': target['min_per_game_hour'],
            'required_min_net_items': None if scope == 'segment' else target['min_net_items'],
            'threshold_passed': rate >= target['min_per_game_hour']
            and (net > 0 if scope == 'segment' else net >= target['min_net_items']),
        }
    return result


def preflight_submission(task, instance_id, submission, server, max_volume=262144):
    """Read-only readiness checks; no fake spawning, loading, clock changes or measurement."""
    result = {
        **validate_submission(task, instance_id, submission, max_volume),
        'validation_scope': 'manifest_and_readonly_world_preflight',
    }
    refs = {stable_hash(ref): ref for out in submission['outputs'] for ref in out['containers']}
    products = [out['product'] for out in submission['outputs']]
    try:
        raw = server.call(
            'observe.containers',
            {
                'dimension': submission['dimension'],
                'products': products,
                'containers': [locator(ref, submission['dimension']) for ref in refs.values()],
            },
        )['result']
        if len(raw['containers']) != len(refs):
            raise RuntimeError('Incomplete container snapshot')
        values = dict(zip(refs, raw['containers']))
        slots, eligible, _ = storage_view(
            submission, values, task.get('shulker_io', {}).get('output', False)
        )
        capacity = (
            allocate_storage(
                slots, eligible, task['storage']['reserved_stack_slots_per_primary']
            )
            if not task.get('inventory_batch')
            else {'passed': True, 'destination': 'evaluator_inventory', 'slots': 36}
        )
        access = output_access(submission, values)
        world = {
            'status': 'observed',
            'storage': capacity,
            'output_access': access,
            'ready': capacity['passed'] and access,
            'measurement_performed': False,
            'world_mutations': 0,
        }
        if task.get('actor_policy', {}).get('presence', 'observer') != 'none':
            site = server.call(
                'observe.afk_site',
                {
                    'position': submission['operation']['afk_position'],
                    'dimension': submission['dimension'],
                },
            )['result']
            world['afk_site'] = site
            if not site.get('loaded') or not site.get('body_clear'):
                world['ready'] = False
            elif not site.get('has_support') and not site.get('fluid_at_feet'):
                world['actor_check_required'] = (
                    'Unsupported site; acceptance requires the actual evaluator actor to be flying'
                )
                if world['ready']:
                    world['ready'] = None
        result['world_preflight'] = world
    except Exception as exc:
        result['world_preflight'] = {
            'status': 'unavailable',
            'ready': None,
            'measurement_performed': False,
            'world_mutations': 0,
            'error': {'type': type(exc).__name__, 'message': str(exc)},
        }
    return result


def sprint_wait(call, ticks, timeout_seconds=120):
    """Run a bounded sprint, then wait for its real completion, not just elapsed ticks."""
    if ticks < 0 or timeout_seconds <= 0:
        raise ValueError('Invalid sprint wait')
    first = call('clock.query')
    started = time.monotonic()
    if first['frozen'] or first['sprinting'] or first['target_tps'] != 20:
        raise RuntimeError('Sprint must start from an idle unfrozen 20 TPS clock')
    if ticks == 0:
        return {'status': 'completed', 'elapsed_game_ticks': 0, 'elapsed_wall_seconds': 0}
    call('clock.sprint', {'ticks': ticks})
    while True:
        last = call('clock.query')
        elapsed = last['tick'] - first['tick']
        if last['frozen'] or last['target_tps'] != 20:
            raise RuntimeError('Clock conditions changed during sprint')
        if not last['sprinting']:
            if elapsed < ticks:
                raise RuntimeError('Sprint stopped before the requested tick window')
            return {
                'status': 'completed',
                'elapsed_game_ticks': elapsed,
                'elapsed_wall_seconds': time.monotonic() - started,
                'initial': first,
                'final': last,
            }
        if time.monotonic() - started >= timeout_seconds:
            raise RuntimeError('Sprint completion timed out')
        time.sleep(0.05)


class Evaluator:
    def __init__(self, session, max_volume=262144, reviewer=None, max_chunks=64):
        self.session = session
        self.max_volume = max_volume
        self.max_chunks = max_chunks
        self.reviewer = reviewer

    def validate(self, instance, submission):
        return validate_submission(
            instance['task'], instance['id'], submission, self.max_volume
        )

    def run(self, instance, submission, tick_mode=None):
        """根据任务验收模式执行测量，返回带任务身份的验收记录。"""
        if trials_for(instance['task']):
            return run_modes(self, instance, submission, tick_mode)
        return self._run_single(instance, submission, tick_mode)

    def _run_single(self, instance, submission, tick_mode=None, _mode_context=None):
        task = instance['task']
        evaluation_id = self.session.run_id
        context = _mode_context or {}
        skip_initial = context.get('skip_initial_checks', False)
        order = (
            context.get('trial') if context.get('trial', {}).get('kind') == 'batch' else None
        )
        reviews = required_review(task)
        needs_heat = not (
            task.get('id') == 'medium.fuel_network.v1'
            and task.get('primary_products') == ['minecraft:bamboo_block']
        )
        directory = ROOT / 'runs' / evaluation_id
        directory.mkdir(parents=True, exist_ok=True)
        report = {
            'protocol_version': VERSION,
            'evaluation_id': evaluation_id,
            'task_instance_id': instance['id'],
            'task_id': task['id'],
            'task_hash': task['task_hash'],
            'submission_hash': stable_hash(submission),
            'verdict': 'inconclusive',
            'goal_passed': None,
            'check_pass_fraction': 0,
            'checks': [],
            'metrics': {},
            'ai_review': None,
            'evidence_path': str(directory),
            'cleanup_errors': [],
        }
        import hashlib

        report['metrics']['evaluator_code_sha256'] = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        report['metrics']['actor_policy_implementation'] = 'observed-lifecycle-v1'
        report['metrics']['actor_monitor_sha256'] = hashlib.sha256(
            Path(__file__).with_name('evaluator_actor.py').read_bytes()
        ).hexdigest()
        evidence = {'task': task, 'submission': submission, 'observations': []}
        tick_mode = task['measurement']['mode'] if tick_mode is None else tick_mode
        wall_budget = max(
            900,
            (task['measurement']['warmup_ticks'] + task['measurement']['window_ticks'])
            / 20
            * 1.5
            + 120,
        )
        evaluation_deadline = time.monotonic() + wall_budget
        report['metrics']['wall_timeout_seconds'] = wall_budget
        actor_guard = None
        report['metrics'].update(
            measurement_mode=tick_mode,
            hour_ticks=72000,
            storage_headroom_ticks=task['storage']['headroom_ticks'],
        )

        def check(name, passed, message, refs=None):
            # The complete observations stay in evidence.json; do not repeat its entire growing ID list in every check.
            paths = (
                refs
                if refs is not None
                else (
                    ['evidence.json#/observations']
                    if evidence['observations']
                    else ['evidence.json#/submission']
                )
            )
            report['checks'].append(
                {
                    'id': name,
                    'status': 'pass' if passed else 'fail',
                    'message': message,
                    'evidence': paths,
                }
            )

        def call(method, params=None, side='server'):
            if time.monotonic() > evaluation_deadline:
                raise RuntimeError(
                    'Infrastructure: acceptance wall-time resource limit exceeded; no task failure inferred'
                )
            step = {
                'method': method,
                'params': params or {},
                'side': side,
                'phase': 'measure',
                'request_id': str(uuid.uuid4()),
            }
            try:
                result = invoke_measurement(
                    self.session,
                    step,
                    evidence['observations'],
                    evaluation_deadline=evaluation_deadline,
                    max_chunks=self.max_chunks,
                )
            except ValueError as exc:
                raise RuntimeError('Invalid native measurement feedback: ' + str(exc)) from exc
            value = result.get('result', result)
            if (
                actor_guard is not None
                and actor_guard.status == 'pass'
                and method not in ('fake_player.status', 'observe.afk_site')
            ):
                actor_guard.sample(force=False)
            return value

        from types import SimpleNamespace

        observed_server = SimpleNamespace(
            call=lambda method, params=None: {'result': call(method, params)}
        )

        def region(prefetched=None, prefetched_player=None):
            harvest_readbacks = []
            wither_readbacks = []
            result = {
                'block_counts': {},
                'inventory_counts': {},
                'loose_item_counts': {},
                'containers': [],
                'unloaded_blocks': 0,
                'entity_counts': {},
                'total_item_counts': {},
                'component_item_counts': [],
                'cooking_totals': {k: {} for k in ('consumed', 'produced', 'fuel')},
                'cooking_nodes': [],
                'furnace_states': [],
                'fuel_item_ids': [],
                'processing_inputs': task.get('processing_inputs', []),
                'inflight_block_counts': {},
                'unticking_blocks': 0,
                'budding_positions': [],
                'encountered_items': [],
                'recipe_observers': [],
            }
            queries = [
                (r, tile) for r in submission_regions(submission) for tile in tiles(r['bounds'])
            ]
            if prefetched is not None and len(prefetched) != len(queries):
                raise RuntimeError('Incomplete atomic region snapshot')
            for index, (r, tile) in enumerate(queries):
                value = (
                    prefetched[index]
                    if prefetched is not None
                    else call('observe.region', {**tile, 'dimension': r['dimension']})
                )
                if task.get('id') == 'hard.nether_stars.v1':
                    source = value.get('wither_source_ledger')
                    if not isinstance(source, dict):
                        raise RuntimeError(
                            'Actual wither-source telemetry unavailable; update the server bridge'
                        )
                    wither_readbacks.append(source)
                if (
                    task.get('processing_inputs')
                    or task.get('natural_source') == 'amethyst_geode'
                ):
                    harvest = value.get('harvest_ledger')
                    if not isinstance(harvest, dict):
                        raise RuntimeError(
                            'Actual block-drop telemetry unavailable; update the server bridge'
                        )
                    harvest_readbacks.append(harvest)
                equipment = value.get('survival_equipment')
                if (
                    not isinstance(equipment, dict)
                    or equipment.get('version') != 'survival-equipment-v1'
                    or not isinstance(equipment.get('passed'), bool)
                ):
                    raise RuntimeError(
                        'Survival equipment telemetry unavailable; update the server bridge'
                    )
                if isinstance(equipment.get('encountered_items'), list):
                    result['encountered_items'].extend(equipment['encountered_items'])
                elif task.get('difficulty') in ('hard', 'expert'):
                    raise RuntimeError(
                        'Nested inventory telemetry unavailable; update the server bridge'
                    )
                check(
                    'survival_equipment',
                    equipment['passed'],
                    'Equipment must have survival-obtainable properties and enchantments',
                )
                if not equipment['passed']:
                    raise ValueError(
                        'Equipment violates shared survival constraints: '
                        + json.dumps(equipment.get('violations', []))
                    )
                result['unloaded_blocks'] += value['unloaded_blocks']
                result['unticking_blocks'] += value.get('unticking_blocks', 0)
                if task.get('natural_source'):
                    policy = value.get('natural_source_policy', {})
                    if (
                        policy.get('version') != 'natural-sources-v1'
                        or not policy.get('natural_world')
                        or not policy.get('creation_disabled')
                    ):
                        raise RuntimeError(
                            'Natural-source protection must be active from fresh world startup'
                        )
                    result['budding_positions'].extend(
                        (r['dimension'], tuple(p)) for p in value.get('budding_positions', [])
                    )
                if (
                    task.get('variant_targets') or (order and order.get('potion'))
                ) and value.get('item_components_version') != 'item-components-v1':
                    raise RuntimeError(
                        'Component stock telemetry unavailable; update the server bridge'
                    )
                result['component_item_counts'].extend(value.get('component_item_counts', []))
                if (
                    task.get('material_conversion')
                    or task.get('material_growth')
                    or task.get('earth_inputs')
                    or task.get('block_output_balance')
                ) and 'inflight_block_counts' not in value:
                    raise RuntimeError('Falling-block stock telemetry unavailable')
                for item, count in value.get('inflight_block_counts', {}).items():
                    result['inflight_block_counts'][item] = (
                        result['inflight_block_counts'].get(item, 0) + count
                    )
                if task.get('processing_inputs') or task.get('earth_inputs'):
                    ledger = value.get('cooking_ledger', {})
                    if ledger.get('version') != 'cooking-ledger-v1' or not ledger.get(
                        'complete'
                    ):
                        raise RuntimeError('Complete cooking-event telemetry unavailable')
                    missing_observers = set(task.get('required_recipe_observers', [])) - set(
                        ledger.get('observers', [])
                    )
                    if missing_observers:
                        raise RuntimeError(
                            'Native recipe observers unavailable: '
                            + ', '.join(sorted(missing_observers))
                        )
                    if result.get('cooking_epoch', ledger['epoch']) != ledger['epoch']:
                        raise RuntimeError('Cooking ledger restarted during readback')
                    result['cooking_epoch'] = ledger['epoch']
                    result['recipe_observers'] = list(
                        set(result['recipe_observers']) | set(ledger.get('observers', []))
                    )
                    if 'furnace_states' not in value or 'fuel_item_ids' not in value:
                        raise RuntimeError('Fuel-state telemetry unavailable')
                    result['furnace_states'].extend(value['furnace_states'])
                    result['fuel_item_ids'] = value['fuel_item_ids']
                    for node in ledger['nodes']:
                        result['cooking_nodes'].append(node)
                        for field in ('consumed', 'produced', 'fuel'):
                            for item, n in node[field].items():
                                result['cooking_totals'][field][item] = (
                                    result['cooking_totals'][field].get(item, 0) + n
                                )
                if task.get('forbidden_initial_entities') and 'entity_counts' not in value:
                    raise RuntimeError('Entity census telemetry unavailable')
                for item, count in value.get('entity_counts', {}).items():
                    result['entity_counts'][item] = result['entity_counts'].get(item, 0) + count
                for key in ('block_counts', 'inventory_counts', 'loose_item_counts'):
                    for item, count in value[key].items():
                        result[key][item] = result[key].get(item, 0) + count
                if (
                    task.get('shulker_io')
                    and value.get('inventory_accounting_version') != 'nested-items-v1'
                ):
                    raise RuntimeError(
                        'Nested stock accounting unavailable; update the server bridge'
                    )
                for item, count in value.get('total_item_counts', {}).items():
                    result['total_item_counts'][item] = (
                        result['total_item_counts'].get(item, 0) + count
                    )
                if value.get('inventory_accounting_version') != 'nested-items-v1':
                    result.pop('total_item_counts', None)
                result['containers'].extend(value['containers'])
            if harvest_readbacks:
                result.update(collect_harvest(harvest_readbacks))
            if wither_readbacks:
                result.update(collect_wither_sources(wither_readbacks))
            result['operating_player_counts'] = {}
            if actor_guard is not None and actor_guard.status == 'pass':
                inventory = (
                    prefetched_player
                    if prefetched is not None
                    else call('fake_player.inventory', {'id': actor_guard.name})
                )
                if not isinstance(inventory, dict):
                    raise RuntimeError('Atomic operating-player stock telemetry unavailable')
                if inventory.get('version') != 'fake-inventory-v1':
                    raise RuntimeError('Operating-player stock telemetry unavailable')
                result['operating_player_counts'] = inventory['counts']
            return result

        products = [o['product'] for o in submission.get('outputs', [])]
        delivery_submission = context.get('original_submission', submission)
        delivery_task = context.get('original_task', task)
        refs = {
            stable_hash(ref): ref
            for ref in submission.get('inputs', [])
            + [r for o in delivery_submission.get('outputs', []) for r in o['containers']]
        }

        def snapshot(prefetched=None):
            keys = list(refs)
            raw = (
                prefetched
                if prefetched is not None
                else call(
                    'observe.containers',
                    {
                        'dimension': submission['dimension'],
                        'products': (
                            list(dict.fromkeys(products + delivery_task['primary_products']))
                            if order
                            else products
                        ),
                        'containers': [locator(refs[k], submission['dimension']) for k in keys],
                    },
                )
            )
            if len(raw['containers']) != len(keys):
                raise RuntimeError('Incomplete atomic storage snapshot')
            values = dict(zip(keys, raw['containers']))
            if task['id'] == 'medium.piglin_bartering.v1':
                validate_barter_components(values, products)
            slots, eligible, counts = storage_view(
                submission, values, task.get('shulker_io', {}).get('output', False)
            )
            delivery = (
                count_components(delivery_task, delivery_submission, values, order)
                if order
                else None
            )
            if order and order.get('potion'):
                counts[order['products'][0]] = delivery['selected']
            return {
                'delivery': delivery,
                'tick': raw['tick'],
                'sample_monotonic_ns': raw['sample_monotonic_ns'],
                'counts': counts,
                'slots': slots,
                'eligible': eligible,
                'containers': values,
            }

        def capture(allow_unloaded=False, include_storage=True):
            query = {
                'regions': [
                    {**tile, 'dimension': r['dimension']}
                    for r in submission_regions(submission)
                    for tile in tiles(r['bounds'])
                ],
                'containers': (
                    [locator(ref, submission['dimension']) for ref in refs.values()]
                    if include_storage
                    else []
                ),
                'products': (
                    list(dict.fromkeys(products + delivery_task['primary_products']))
                    if order
                    else products
                ),
            }
            if actor_guard is not None and actor_guard.status == 'pass':
                query['actor'] = actor_guard.name
            try:
                raw = call('observe.evaluation_snapshot', query)
                if (
                    not isinstance(raw, dict)
                    or not isinstance(raw.get('regions'), list)
                    or not isinstance(raw.get('storage'), dict)
                ):
                    raise RuntimeError(
                        'Atomic evaluation snapshots unavailable; update the server bridge'
                    )
            except Exception:
                if not getattr(self.session, '_legacy_evaluation_snapshot_fixture', False):
                    raise
                report['metrics']['atomic_snapshot'] = 'legacy_test_fixture_only'
                r = region()
                if not include_storage:
                    return r, None
                return r, (
                    None
                    if allow_unloaded and (r['unloaded_blocks'] or r['unticking_blocks'])
                    else (
                        {'containers': {}, 'slots': {}, 'eligible': {}}
                        if task.get('inventory_batch')
                        else snapshot()
                    )
                )
            report['metrics']['atomic_snapshot'] = 'native_single_server_thread'
            r = region(raw['regions'], raw.get('player_inventory'))
            if r['unloaded_blocks'] or r['unticking_blocks']:
                if allow_unloaded:
                    return r, None
                raise RuntimeError('Machine regions stopped ticking during acceptance')
            if not include_storage:
                return r, None
            return r, (
                {'containers': {}, 'slots': {}, 'eligible': {}}
                if task.get('inventory_batch')
                else snapshot(raw['storage'])
            )

        try:
            validate_submission(task, instance['id'], submission, self.max_volume)
            if task.get('machine_limits'):
                report['metrics']['machine_bounds'] = validate_machine_limits(task, submission)
                check(
                    'machine_bounds',
                    True,
                    'The complete declared machine, including collection, packing and storage, fits its common spatial envelope',
                )
            missing = set(
                task.get('acceptance', {}).get('required_checks', [])
            ) - available_checks(task)
            if order:
                missing -= {'configurable_batches', 'brew_orders', 'item_components'}
            missing -= set(reviews)
            if reviews and self.reviewer is None:
                report['checks'].append(
                    {
                        'id': 'required_structural_review',
                        'status': 'inconclusive',
                        'message': 'Hybrid evaluation is required to review shared machinery using native review-volume evidence',
                        'evidence': ['evidence.json#/submission/review_volumes'],
                    }
                )
                return report
            if missing:
                report['checks'].append(
                    {
                        'id': 'evaluator_capabilities',
                        'status': 'inconclusive',
                        'message': 'Required evaluator checks are not implemented: '
                        + ', '.join(sorted(missing)),
                        'evidence': ['evidence.json#/task/acceptance'],
                    }
                )
                return report
            if tick_mode not in task['measurement']['allowed_modes']:
                raise ValueError('Tick mode is not permitted by this task')
            maintained_weather = 'head_weather_conditions' in task.get('acceptance', {}).get(
                'required_checks', []
            )
            if maintained_weather and submission['dimension'] != 'minecraft:overworld':
                raise ValueError('Head production weather conditions require the Overworld')
            check(
                'manifest_complete',
                True,
                'All required fields, products, roles, bounds and operation declarations are present',
            )
            clock = call('clock.query')
            if clock['frozen'] or clock['sprinting'] or clock['target_tps'] != 20:
                check(
                    'initial_clock',
                    False,
                    'Evaluation must start with an idle unfrozen 20 TPS target; only evaluator-owned sprint is permitted',
                )
                return report
            check(
                'initial_clock',
                True,
                'Idle unfrozen 20 TPS target verified; selected tick mode: ' + tick_mode,
            )
            call('clock.freeze')
            if maintained_weather:
                # These mutations use the normal run-owned environment guard;
                # session cleanup restores the original weather and gamerules.
                weather_context = {'dimension': 'minecraft:overworld'}
                call(
                    'command.run',
                    {
                        'text': 'gamerule minecraft:advance_weather false',
                        'context': weather_context,
                    },
                )
                call('command.run', {'text': 'weather thunder', 'context': weather_context})
            bounds = submission['bounds']
            regions = submission_regions(submission)
            chunk_keys = {
                (r['dimension'], x, z)
                for r in regions
                for x in range(r['bounds']['min'][0] // 16, r['bounds']['max'][0] // 16 + 1)
                for z in range(r['bounds']['min'][2] // 16, r['bounds']['max'][2] // 16 + 1)
            }
            chunks = len(chunk_keys)
            if chunks > self.max_chunks:
                raise ValueError(
                    f'Declared region exceeds the evaluator loading budget of {self.max_chunks} chunks'
                )
            for r in regions:
                b = r['bounds']
                call(
                    'command.run',
                    {
                        'text': f"forceload add {b['min'][0]} {b['min'][2]} {b['max'][0]} {b['max'][2]}",
                        'context': {'dimension': r['dimension']},
                    },
                )
            deadline = time.monotonic() + 60
            while True:
                initial_region, initial_snapshot = capture(allow_unloaded=True)
                if (
                    not initial_region['unloaded_blocks']
                    and not initial_region['unticking_blocks']
                ):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Declared region did not finish loading')
                time.sleep(0.2)
            check(
                'site_loaded',
                initial_region['unloaded_blocks'] == 0
                and initial_region['unticking_blocks'] == 0,
                'Every declared machine region is loaded and ticking',
            )
            if initial_region['unloaded_blocks']:
                return report
            if task.get('_suite_balance_items'):
                report['metrics']['suite_initial_stocks'] = {
                    item: item_stock(initial_region, item)
                    + initial_region['operating_player_counts'].get(item, 0)
                    for item in task['_suite_balance_items']
                }
            if task.get('natural_source'):
                kind = task['natural_source']
                if kind in ('monument', 'trial_chambers'):
                    sources = [
                        p for p in submission['inspection_points'] if p['role'] == 'source'
                    ]
                    if not sources:
                        raise ValueError(
                            'Declare an inspection point at the original natural source'
                        )
                    proven = []
                    for point in sources:
                        value = call(
                            'observe.natural_source',
                            {
                                **locator(point, submission['dimension']),
                                'structure': 'minecraft:' + kind,
                            },
                        )
                        proven.append(
                            value.get('loaded')
                            and value.get('natural_world')
                            and value.get('valid_structure')
                        )
                    source_ok = any(proven)
                    if kind == 'trial_chambers':
                        source_ok = (
                            source_ok
                            and initial_region['block_counts'].get('minecraft:trial_spawner', 0)
                            > 0
                        )
                elif kind == 'bedrock':
                    source_ok = initial_region['block_counts'].get('minecraft:bedrock', 0) > 0
                else:
                    source_ok = bool(initial_region['budding_positions'])
                check(
                    'natural_source_provenance',
                    source_ok,
                    'The farm uses the original natural source in a fresh world with protected-source creation disabled',
                )
                if not source_ok:
                    return report
            if (
                (task.get('processing_inputs') or task.get('earth_inputs'))
                and 'fuel_autonomy' in task.get('acceptance', {}).get('required_checks', [])
                and needs_heat
                and not skip_initial
            ):
                startup = cold_start(
                    initial_region,
                    task.get('earth_inputs', task.get('processing_inputs', [])),
                    item_stock,
                )
                report['metrics']['fuel_cold_start'] = startup
                check(
                    'fuel_cold_start',
                    startup['passed'],
                    'No preloaded fuel or burning furnace is allowed; declared raw feedstock stays at the single input',
                )
                if not startup['passed']:
                    return report
            if task.get('difficulty') in ('hard', 'expert') and not skip_initial:
                allowed = 'minecraft:tnt' not in initial_region['encountered_items']
                check(
                    'initial_inventory_policy',
                    allowed,
                    'Hard and expert tasks prohibit preloaded TNT items, including nested inventories; placed TNT blocks are permitted',
                )
                if not allowed:
                    return report
            if task.get('forbidden_blocks'):
                blocks_ok = not any(
                    initial_region['block_counts'].get(b, 0) for b in task['forbidden_blocks']
                )
                check(
                    'forbidden_blocks',
                    blocks_ok,
                    'Declared machine regions must not contain prohibited blocks',
                )
                if not blocks_ok:
                    return report
            if task.get('forbidden_initial_entities') and not skip_initial:
                allowed = not any(
                    initial_region['entity_counts'].get(kind, 0)
                    for kind in task['forbidden_initial_entities']
                )
                check(
                    'initial_entity_policy',
                    allowed,
                    'Initial entities must satisfy the task source restrictions',
                )
                if not allowed:
                    return report
            for volume in submission.get('review_volumes', []):
                call('observe.blocks', {**volume['bounds'], 'dimension': volume['dimension']})
            evidence['inspection_points'] = [
                call(
                    'observe.blocks',
                    {
                        'min': p['position'],
                        'max': p['position'],
                        'dimension': p.get('dimension', submission['dimension']),
                    },
                )
                for p in submission['inspection_points']
            ]
            mechanism_ok = all(
                any(initial_region['block_counts'].get(block, 0) > 0 for block in group)
                for group in task['mechanism_groups']
            )
            # Waterlogged supports count as a real water probe even when no
            # standalone water block exists in the submitted machine bounds.
            if not mechanism_ok and any(
                'waterlogged=true' in str(v) for v in evidence['inspection_points']
            ):
                mechanism_ok = all(
                    'minecraft:water' in group
                    or any(initial_region['block_counts'].get(b, 0) > 0 for b in group)
                    for group in task['mechanism_groups']
                )
            check(
                'mechanism_observation',
                mechanism_ok,
                'Task-required renewable source/crop groups exist; no fixed schematic or dimensions imposed',
            )
            batch = task.get('inventory_batch')
            before = (
                initial_snapshot
                if not batch
                else {'containers': {}, 'slots': {}, 'eligible': {}}
            )
            if task.get('shulker_io', {}).get('input') and not skip_initial:
                initial_slots = [
                    s
                    for ref in submission['inputs']
                    for s in before['containers'][stable_hash(ref)]['slots']
                    if s['count']
                ]
                packed = bool(initial_slots) and all(
                    s['item'].endswith('shulker_box') and 'shulker_slots' in s
                    for s in initial_slots
                )
                packed = packed and any(
                    child['count']
                    for s in initial_slots
                    for child in s.get('shulker_slots', [])
                )
                check(
                    'shulker_input',
                    packed,
                    'Initial feedstock must arrive in packed shulker boxes at the declared input; the machine must unpack it automatically',
                )
                if not packed:
                    return report
            if (
                'single_input' in task.get('acceptance', {}).get('required_checks', [])
                and not skip_initial
            ):
                feedstocks = task.get('single_input_items')
                if not feedstocks:
                    raise RuntimeError(
                        'Single-input feedstock accounting is not configured for this profile'
                    )
                input_slots = [
                    slot
                    for ref in submission['inputs']
                    for slot in before['containers'][stable_hash(ref)].get('slots', [])
                ]
                input_slots = input_slots + [
                    child for slot in input_slots for child in slot.get('shulker_slots', [])
                ]
                entry = {
                    item: sum(s['count'] for s in input_slots if s['item'] == item)
                    for item in feedstocks
                }
                total = {item: item_stock(initial_region, item) for item in feedstocks}
                report['metrics']['input_initial'] = {'entry': entry, 'system': total}
                check(
                    'single_input',
                    entry == total,
                    'All preloaded feedstock must be in the single declared input container',
                )
                if entry != total:
                    return report
            check(
                'output_access_initial',
                batch is not None or output_access(submission, before['containers']),
                'Every output chest has vanilla lid clearance on both halves, including sitting-cat obstruction',
            )
            if report['checks'][-1]['status'] == 'fail':
                return report
            capacity = (
                allocate_storage(
                    before['slots'],
                    before['eligible'],
                    task['storage']['reserved_stack_slots_per_primary'],
                )
                if not batch
                else {'passed': True, 'destination': 'evaluator_inventory', 'slots': 36}
            )
            report['metrics']['storage_initial'] = capacity
            check(
                'storage_capacity',
                capacity['passed'],
                'Each primary product has the task-required number of distinct compatible stack slots',
            )
            if not capacity['passed'] or not mechanism_ok:
                return report
            # Instrumentation creates a leased player at the declared AFK point.
            # It never interprets arbitrary startup text as executable instructions.
            if context:
                apply_controls(
                    call, context['original_submission'], context['binding'], 'select'
                )
            op = submission['operation']
            fake = 'ev' + hashlib.sha256(evaluation_id.encode()).hexdigest()[:12]
            spawn = {
                'id': fake,
                'x': op['afk_position'][0],
                'y': op['afk_position'][1],
                'z': op['afk_position'][2],
                'dimension': submission['dimension'],
            }
            if op['target'] is not None:
                dx, dy, dz = [t - p for t, p in zip(op['aim_point'], op['afk_position'])]
                dy -= 1.62
                spawn.update(
                    yaw=math.degrees(math.atan2(-dx, dz)),
                    pitch=-math.degrees(math.atan2(dy, math.hypot(dx, dz))),
                )
            policy = task.get('actor_policy')
            needs_player = policy is None or policy['presence'] != 'none'
            report['metrics']['actor_policy'] = policy or {
                'presence': 'legacy',
                'driver': op['driver'],
            }
            if needs_player:
                call('fake_player.spawn', spawn)
            if needs_player:
                call(
                    'command.run',
                    {'text': 'gamemode survival ' + fake, 'keep_conditions': True},
                )
                actor_guard = EvaluatorActor(
                    call, fake, op['afk_position'], submission['dimension']
                )
                actor_guard.start()
            if needs_player and op['tool'] is not None:
                if not batch and op['tool'] not in task.get('material_driver_items', []):
                    if op.get('tool_components'):
                        call(
                            'fake_player.equip',
                            {
                                'id': fake,
                                'item': op['tool'],
                                'components': op['tool_components'],
                            },
                        )
                    else:
                        call('command.run', {'text': 'give ' + fake + ' ' + op['tool'] + ' 1'})
            seed_before = None
            resource_before = None
            player_inventory_before = None
            if (
                'player_action_inventory'
                in task.get('acceptance', {}).get('required_checks', [])
                and not batch
            ):
                if not needs_player:
                    raise RuntimeError(
                        'Player inventory accounting requires an operating player'
                    )
                player_inventory_before = call('fake_player.inventory', {'id': fake})
                if player_inventory_before.get('version') != 'fake-inventory-v1':
                    raise RuntimeError('Player inventory telemetry unavailable')
            balance_items = set(
                task.get('replanting_items', []) + task.get('resource_balance_items', [])
            )
            if balance_items:
                seed_region, _ = capture(include_storage=False)
                seed_inventory = {
                    'version': 'fake-inventory-v1',
                    'counts': seed_region['operating_player_counts'],
                }
                if seed_inventory.get('version') != 'fake-inventory-v1':
                    raise RuntimeError('Player inventory telemetry unavailable')
                stocks = {
                    item: item_stock(seed_region, item) + seed_inventory['counts'].get(item, 0)
                    for item in balance_items
                }
                if task.get('resource_balance_include_blocks'):
                    for item in task.get('resource_balance_items', []):
                        stocks[item] += seed_region['block_counts'].get(item, 0)
                if task.get('replanting_items'):
                    seed_before = {item: stocks[item] for item in task['replanting_items']}
                if task.get('resource_balance_items'):
                    resource_before = {
                        item: stocks[item] for item in task['resource_balance_items']
                    }
            if not batch and needs_player and op['driver'] != 'none':
                if op.get('offhand_item'):
                    call(
                        'fake_player.use_with_offhand',
                        {'id': fake, 'mode': 'continuous', 'item': op['offhand_item']},
                    )
                elif op['tool'] in task.get('material_driver_items', []):
                    call(
                        'fake_player.use_material',
                        {'id': fake, 'mode': 'continuous', 'item': op['tool']},
                    )
                else:
                    call('fake_player.' + op['driver'], {'id': fake, 'mode': 'continuous'})
            if op['driver'] == 'attack_and_use':
                status = call('fake_player.inventory', {'id': fake}).get(
                    'combined_operation', {}
                )
                if status.get('version') != 'combined-operation-v1':
                    raise RuntimeError('Combined player operation telemetry unavailable')
            actors = call('observe.actors')
            operator = next((p for p in actors if p['name'] == fake), None)
            afk_ok = not needs_player or (
                operator is not None
                and operator['dimension'] == submission['dimension']
                and math.dist(operator['position'], op['afk_position']) < 1
            )
            check(
                'afk_position',
                afk_ok,
                'Task-required observer is present, or this task needs no evaluator player',
            )
            if not afk_ok:
                return report
            if policy:
                foreign = [p['name'] for p in actors if p.get('fake') and p['name'] != fake]
                check(
                    'no_extra_fake_players_initial',
                    not foreign,
                    'No external fake players may supply undeclared work: ' + str(foreign),
                )
                if foreign:
                    return report
            conditions = call('experiment.conditions')
            evidence['conditions'] = conditions

            def find_rule(value):
                if isinstance(value, dict):
                    if 'minecraft:random_tick_speed' in value:
                        return value['minecraft:random_tick_speed']
                    for child in value.values():
                        found = find_rule(child)
                        if found is not None:
                            return found
                if isinstance(value, list):
                    for child in value:
                        found = find_rule(child)
                        if found is not None:
                            return found
                return None

            rule_scopes = [
                next(
                    (
                        w
                        for w in conditions.get('worlds', [])
                        if w.get('dimension') == dimension
                    ),
                    conditions,
                )
                for dimension in {r['dimension'] for r in regions}
            ]
            rule_ok = all(
                find_rule(scope) == task['measurement']['random_tick_speed']
                for scope in rule_scopes
            )
            check(
                'random_tick_speed',
                rule_ok,
                'Default random tick speed required; no evaluator acceleration',
            )
            if not rule_ok:
                return report
            if not skip_initial and submission.get('start_controls'):
                apply_controls(
                    call, submission, {'controls': submission['start_controls']}, 'select'
                )
                apply_controls(
                    call, submission, {'controls': submission['start_controls']}, 'trigger'
                )
            if context and not order:
                apply_controls(
                    call, context['original_submission'], context['binding'], 'trigger'
                )
            call('clock.unfreeze')
            warmup_timeout = task['measurement']['warmup_ticks'] / 20 * 3 + 60
            wait = (
                sprint_wait(call, task['measurement']['warmup_ticks'], warmup_timeout)
                if tick_mode == 'sprint'
                else natural_wait(
                    observed_server,
                    task['measurement']['warmup_ticks'],
                    warmup_timeout,
                    require_natural=True,
                )
            )
            if wait['status'] != 'completed':
                raise RuntimeError(wait)
            if maintained_weather:
                weather_ok = maintained_thunderstorm(call('experiment.conditions'))
                check(
                    'head_weather_conditions',
                    weather_ok,
                    'The environment maintains Overworld thunderstorm conditions with weather progression disabled',
                )
                if not weather_ok:
                    return report
            if actor_guard is not None:
                actor_guard.sample()
            if batch:
                if __package__:
                    from environments.techmc.acceptance.inventory import measure_bucket_batch
                else:
                    from acceptance.inventory import measure_bucket_batch
                measure_bucket_batch(
                    task,
                    products[0],
                    fake,
                    call,
                    region,
                    check,
                    report,
                    tick_mode,
                    sprint_wait,
                    lambda ticks, timeout: natural_wait(
                        observed_server, ticks, timeout, require_natural=True
                    ),
                )
                if self.reviewer:
                    report['ai_review'] = self.reviewer.review(
                        task, submission, evidence, report
                    )
                    check(
                        'ai_evidence_review',
                        report['ai_review']['verdict'] == 'pass',
                        'Additional AI evidence review',
                    )
                return report
            start_region, start = capture()
            if order:
                apply_controls(
                    call, context['original_submission'], context['binding'], 'trigger'
                )
            variants = task.get('variant_targets', [])
            variant_task = copy.deepcopy(task)
            variant_task['measurement']['product_targets'] = {
                t['id']: t['target'] for t in variants
            }

            def variant_snapshot(s):
                return {
                    **s,
                    'counts': classified_counts(
                        variants, submission['variant_outputs'], s['containers'], stable_hash
                    ),
                }

            variant_start = variant_snapshot(start) if variants else None
            variant_debt = {
                v['id']: max(
                    0,
                    variant_stock(variants, start_region)[v['id']]
                    - variant_start['counts'][v['id']],
                )
                for v in variants
            }
            variant_previous = variant_start
            variant_carry = dict(variant_debt)
            variant_segments = []
            inflight = {
                p: max(0, item_stock(start_region, p) - start['counts'][p]) for p in products
            }
            if order and order.get('potion'):
                matches = sum(
                    row['count']
                    for row in start_region['component_item_counts']
                    if row['item'] == order['products'][0]
                    and row['components'].get('minecraft:potion_contents', {}).get('potion')
                    == order['potion']
                    and row['components']
                    .get('minecraft:potion_contents', {})
                    .get('custom_effect_count')
                    == 0
                )
                inflight[order['products'][0]] = max(
                    0, matches - start['counts'][order['products'][0]]
                )
            if task['id'] == 'hard.nether_stars.v1':
                inflight['minecraft:nether_star'] += start_region['entity_counts'].get(
                    'minecraft:wither', 0
                )
            timeout = task['measurement']['window_ticks'] / 20 * 3 + 60
            segment_count = task['measurement'].get('segments', 1)
            segment_ticks = task['measurement']['window_ticks'] // segment_count
            segment_start = start
            carry = dict(inflight)
            segment_metrics = []
            aggregate_segments = []
            previous_material_region = start_region
            material_segments = []
            previous_earth_region = start_region
            earth_segments = []
            previous_amethyst_region = start_region
            amethyst_segments = []
            amethyst_debt = 0
            previous_world_region = start_region
            world_segments = []
            previous_furnace_region = start_region
            furnace_segments = []
            for _ in range(segment_count):
                wait = (
                    sprint_wait(call, segment_ticks, timeout)
                    if tick_mode == 'sprint'
                    else natural_wait(
                        observed_server, segment_ticks, timeout, require_natural=True
                    )
                )
                if wait['status'] != 'completed':
                    raise RuntimeError(wait)
                if maintained_weather:
                    weather_ok = maintained_thunderstorm(call('experiment.conditions'))
                    check(
                        'head_weather_conditions',
                        weather_ok,
                        'Thunderstorm conditions remain active throughout the measurement windows',
                    )
                    if not weather_ok:
                        return report
                if actor_guard is not None:
                    actor_guard.sample()
                end_region, end = capture()
                if 'furnaces' in task.get('machine_limits', {}):
                    furnace_segments.append(
                        furnace_activity(
                            task['machine_limits'],
                            previous_furnace_region,
                            end_region,
                            segment_ticks,
                            products[0],
                        )
                    )
                    previous_furnace_region = end_region
                if task.get('natural_source') == 'amethyst_geode':
                    old = amethyst_ineligible_drops(
                        start['tick'],
                        previous_amethyst_region,
                        end_region,
                        start_sequence=start_region['harvest_sequence'],
                    )
                    amethyst_segments.append(old)
                    amethyst_debt += old['ineligible_count']
                    carry['minecraft:amethyst_shard'] += old['ineligible_count']
                    inflight['minecraft:amethyst_shard'] += old['ineligible_count']
                    previous_amethyst_region = end_region
                if variants:
                    variant_end = variant_snapshot(end)
                    variant_segments.append(
                        production_metrics(
                            variant_task,
                            variant_previous,
                            variant_end,
                            variant_carry,
                            scope='segment',
                        )
                    )
                    variant_carry = {
                        v: max(
                            0,
                            variant_carry[v]
                            - (variant_end['counts'][v] - variant_previous['counts'][v]),
                        )
                        for v in variant_carry
                    }
                    variant_previous = variant_end
                segment_metrics.append(
                    production_metrics(task, segment_start, end, carry, scope='segment')
                )
                if task.get('material_growth'):
                    current_material_region = end_region
                    material_segments.append(
                        material_growth_balance(
                            task['material_growth'],
                            previous_material_region,
                            current_material_region,
                            sum(m['net_new_collected'] for m in segment_metrics[-1].values()),
                            item_stock,
                            task['id'] == 'hard.granular_materials.v1',
                        )
                    )
                    previous_material_region = current_material_region
                if task.get('block_output_balance'):
                    world_segments.append(
                        world_output_balance(
                            products,
                            previous_world_region,
                            end_region,
                            {p: m['net_new_collected'] for p, m in segment_metrics[-1].items()},
                            item_stock,
                        )
                    )
                    previous_world_region = end_region
                if task.get('earth_inputs'):
                    earth_segments.append(
                        earth_new_output(
                            previous_earth_region,
                            end_region,
                            {p: m['net_new_collected'] for p, m in segment_metrics[-1].items()},
                            item_stock,
                        )
                    )
                    previous_earth_region = end_region
                aggregate_segments.append(
                    aggregate_output_metrics(task, segment_metrics[-1], scope='segment')
                )
                # Carry initial in-flight debt across segments; do not repeatedly
                # subtract it or let a reservoir pass as newly generated output.
                carry = {
                    p: max(0, carry[p] - (end['counts'][p] - segment_start['counts'][p]))
                    for p in products
                }
                segment_start = end
            if order:
                settle = delivery_task.get('order_settle_ticks', 200)
                wait = (
                    sprint_wait(call, settle, max(60, settle / 20 * 3))
                    if tick_mode == 'sprint'
                    else natural_wait(
                        observed_server, settle, max(60, settle / 20 * 3), require_natural=True
                    )
                )
                if wait['status'] != 'completed':
                    raise RuntimeError(wait)
                end_region, settled = capture()
                delivery = exact_delivery(
                    start['delivery'], settled['delivery'], order['quantity']
                )
                report['metrics']['batch_delivery'] = delivery
                check(
                    'exact_batch_delivery',
                    delivery['passed'],
                    'Deliver exactly the requested batch and no other product or potion variant, including the post-order settling interval',
                )
            for volume in submission.get('review_volumes', []):
                call('observe.blocks', {**volume['bounds'], 'dimension': volume['dimension']})
            final_region = end_region
            if task.get('_suite_balance_items'):
                report['metrics']['suite_final_stocks'] = {
                    item: item_stock(final_region, item)
                    + final_region['operating_player_counts'].get(item, 0)
                    for item in task['_suite_balance_items']
                }
            if op['driver'] == 'attack_and_use':
                status = call('fake_player.inventory', {'id': fake}).get(
                    'combined_operation', {}
                )
                if status.get('version') != 'combined-operation-v1':
                    raise RuntimeError('Combined player operation telemetry unavailable')
                report['metrics']['combined_operation'] = status
                check(
                    'combined_player_operation',
                    not status.get('error'),
                    'The same stationary player attacks and drinks only automatically delivered bottles',
                )
                if (
                    task['id'] == 'hard.raid.v1'
                    or task.get('selected_profile') == 'ominous_keys'
                ):
                    check(
                        'consumable_accounting',
                        status.get('bottles_drunk', 0) > 0,
                        'Vanilla item-use statistics confirm ominous bottle consumption; no evaluator replenishment',
                    )
                    check(
                        'item_components',
                        not status.get('error'),
                        'Consumed ominous bottles use survival-valid amplifiers',
                    )
                if task['id'] == 'hard.raid.v1':
                    check(
                        'raid_operator',
                        status.get('raids_triggered', 0) > 0,
                        'Vanilla raid creation was triggered by the same operating player',
                    )
            if task.get('natural_source') == 'amethyst_geode':
                report['metrics']['amethyst_initial_cluster_debt'] = {
                    'excluded_actual_shards': amethyst_debt,
                    'segments': amethyst_segments,
                }
                check(
                    'initial_cluster_output',
                    True,
                    'Actual shard drops from clusters already mature at measurement start are excluded, including real Fortune yields',
                )
                check(
                    'protected_sources',
                    set(initial_region['budding_positions'])
                    == set(final_region['budding_positions']),
                    'Original budding amethyst sources remain in place throughout operation',
                )
            if task['id'] == 'hard.nether_stars.v1':
                balance = wither_recipe_balance(initial_region, final_region, item_stock)
                report['metrics']['recipe_accounting'] = balance
                check(
                    'recipe_accounting',
                    balance['passed'],
                    'Every new star or live summoned wither must consume exactly four soul blocks and three wither skeleton skulls',
                )
            if task.get('material_conversion'):
                balance = conversion_balance(
                    task['material_conversion'], initial_region, final_region, item_stock
                )
                report['metrics']['recipe_accounting'] = balance
                check(
                    'recipe_accounting',
                    balance['passed'],
                    'Input, placed blocks, falling blocks and collected output must match the vanilla material conversion',
                )
            if task.get('earth_inputs'):
                ledger = earth_mass_balance(
                    initial_region, final_region, item_stock, products=products
                )
                supplies = earth_operating_supplies(
                    initial_region, final_region, item_stock, check_startup=not skip_initial
                )
                report['metrics']['recipe_accounting'] = ledger
                report['metrics']['operating_resources'] = supplies
                check(
                    'recipe_accounting',
                    ledger['passed'],
                    'Earth materials and actual vanilla crafts must conserve the complete raw, intermediate and output quantities',
                )
                check(
                    'resource_balance',
                    ledger['passed'],
                    'No earth material or auxiliary ingredient may disappear from the system ledger',
                )
                for name in ('automatic_operating_supplies', 'operating_resources'):
                    if name in task.get('acceptance', {}).get('required_checks', []):
                        check(
                            name,
                            supplies['passed'],
                            'Bottles are automatically supplied and preserved; prefilled potions cannot replace automatic water supply',
                        )
                if 'fuel_autonomy' in task.get('acceptance', {}).get('required_checks', []):
                    if 'minecraft:terracotta' in products:
                        fuel = autonomous_fuel(start_region, final_region, task['earth_inputs'])
                        report['metrics']['fuel_autonomy'] = fuel
                        check(
                            'fuel_autonomy',
                            fuel['passed'],
                            'Terracotta mode must consume automatically supplied fuel after warmup',
                        )
                    else:
                        report['checks'].append(
                            {
                                'id': 'fuel_autonomy',
                                'status': 'not_applicable',
                                'message': 'This mode does not burn fuel; the terracotta trial validates fuel autonomy',
                                'evidence': [],
                            }
                        )
            if task.get('processing_inputs') and not task.get('earth_inputs'):
                if 'furnaces' in task.get('machine_limits', {}):
                    report['metrics']['furnace_array'] = {'segments': furnace_segments}
                    check(
                        'furnace_array',
                        bool(furnace_segments) and all(s['passed'] for s in furnace_segments),
                        'Every installed furnace must sustain its minimum individual recipe throughput in every segment',
                    )
                ledger = cooking_balance(initial_region, final_region, item_stock)
                report['metrics']['recipe_accounting'] = ledger
                check(
                    'recipe_accounting',
                    ledger['passed'],
                    'Actual vanilla recipe events, fuel consumption and all remaining inventories must balance without material loss',
                )
                if 'fuel_autonomy' in task.get('acceptance', {}).get('required_checks', []):
                    if needs_heat:
                        fuel = autonomous_fuel(
                            start_region, final_region, task['processing_inputs']
                        )
                        report['metrics']['fuel_autonomy'] = fuel
                        check(
                            'fuel_autonomy',
                            fuel['passed'],
                            'Fuel consumed after warmup must be automatically supplied; directly burning preloaded raw feedstock after warmup is not fuel production',
                        )
                    else:
                        report['checks'].append(
                            {
                                'id': 'fuel_autonomy',
                                'status': 'not_applicable',
                                'message': 'The selected vanilla crafting recipe needs no heat or operating fuel',
                                'evidence': [],
                            }
                        )
            if task.get('forbidden_blocks'):
                check(
                    'forbidden_blocks_final',
                    not any(
                        final_region['block_counts'].get(b, 0) for b in task['forbidden_blocks']
                    ),
                    'Prohibited blocks remain absent at measurement end',
                )
            if player_inventory_before is not None:
                player_inventory_after = call('fake_player.inventory', {'id': fake})
                if player_inventory_after.get('version') != 'fake-inventory-v1':
                    raise RuntimeError('Player inventory telemetry unavailable')
                report['metrics']['player_inventory'] = {
                    'before': player_inventory_before,
                    'after': player_inventory_after,
                    'replenishment': 'No evaluator replacement after startup',
                }
                check(
                    'player_action_inventory',
                    True,
                    'Observed operating inventory; ordinary consumable use and wear are permitted',
                )
            if task.get('single_input_items'):
                report['metrics']['input_final'] = {
                    item: item_stock(final_region, item) for item in task['single_input_items']
                }
            if balance_items:
                seed_inventory = {
                    'version': 'fake-inventory-v1',
                    'counts': final_region['operating_player_counts'],
                }
                if seed_inventory.get('version') != 'fake-inventory-v1':
                    raise RuntimeError('Player inventory telemetry unavailable')
                stocks = {
                    item: item_stock(final_region, item) + seed_inventory['counts'].get(item, 0)
                    for item in balance_items
                }
                if task.get('resource_balance_include_blocks'):
                    for item in task.get('resource_balance_items', []):
                        stocks[item] += final_region['block_counts'].get(item, 0)
            if seed_before is not None:
                seed_after = {item: stocks[item] for item in seed_before}
                growth = task.get('replanting_growth', {})
                relevant = (
                    {op['tool']} if growth and op['tool'] in seed_before else set(seed_before)
                )
                deltas = {
                    item: (
                        0
                        if item == 'minecraft:jungle_sapling'
                        and op.get('planting_footprint') == 'single'
                        else growth.get('minimum_net_increase', 0)
                    )
                    for item in relevant
                }
                report['metrics']['seed_balance'] = {
                    'before': seed_before,
                    'after': seed_after,
                    'required_net_change': deltas,
                }
                check(
                    'seed_balance',
                    all(
                        seed_after[item] >= seed_before[item] + delta
                        for item, delta in deltas.items()
                    ),
                    'Total replanting stock must meet the task growth requirement, including the evaluator inventory',
                )
            if resource_before is not None:
                resource_after = {item: stocks[item] for item in resource_before}
                report['metrics']['resource_balance'] = {
                    'before': resource_before,
                    'after': resource_after,
                }
                minimum = task.get('resource_balance_min_increase', 1)
                report['metrics']['resource_balance']['minimum_increase'] = minimum
                check(
                    'resource_growth',
                    all(
                        resource_after[item] - count >= minimum
                        for item, count in resource_before.items()
                    ),
                    'Total system resources must meet the required net increase from operation startup',
                )
            if segment_count > 1:
                report['metrics']['production_segments'] = segment_metrics
                if task['mode'] == 'open_efficiency':
                    report['checks'].append(
                        {
                            'id': 'sustained_production',
                            'status': 'not_applicable',
                            'message': 'Open efficiency has no minimum segment rate',
                            'evidence': [],
                        }
                    )
                else:
                    check(
                        'sustained_production',
                        all(
                            v['threshold_passed']
                            for segment in segment_metrics
                            for v in segment.values()
                        ),
                        'Every segment must sustain the task rate for every primary product',
                    )
            check(
                'output_access_final',
                output_access(submission, end['containers']),
                'Output chest lid clearance remains usable after production',
            )
            if policy:
                final_actors = call('observe.actors')
                check(
                    'no_extra_fake_players_final',
                    not any(p.get('fake') and p['name'] != fake for p in final_actors),
                    'No external fake players at measurement end',
                )
            if 'integrated_operation' in task.get('acceptance', {}).get('required_checks', []):
                check(
                    'integrated_operation',
                    not needs_player
                    or (actor_guard is not None and actor_guard.status == 'pass'),
                    'At most one player operates at the same declared position; moving between manual stations is forbidden',
                )
            report['metrics']['production'] = production_metrics(task, start, end, inflight)
            if task.get('id') == 'hard.nether_stars.v1':
                proof = natural_wither_output(
                    start_region,
                    final_region,
                    report['metrics']['production']['minecraft:nether_star'][
                        'net_new_collected'
                    ],
                )
                report['metrics']['wither_source_provenance'] = proof
                check(
                    'natural_wither_deaths',
                    proof['passed'],
                    'Credited stars require actual vanilla-pattern wither deaths near original natural bedrock, with no untracked source deaths',
                )
            if task.get('block_output_balance'):
                balance = world_output_balance(
                    products,
                    start_region,
                    final_region,
                    {
                        p: m['net_new_collected']
                        for p, m in report['metrics']['production'].items()
                    },
                    item_stock,
                )
                report['metrics']['world_output_balance'] = {
                    'total': balance,
                    'segments': world_segments,
                }
                check(
                    'world_output_balance',
                    balance['passed'] and all(s['passed'] for s in world_segments),
                    'Every product must grow across world blocks, falling blocks, inventory and the operating player by at least its credited collected output in every segment',
                )
            if task.get('earth_inputs'):
                growth = earth_new_output(
                    start_region,
                    final_region,
                    {
                        p: m['net_new_collected']
                        for p, m in report['metrics']['production'].items()
                    },
                    item_stock,
                )
                report['metrics']['earth_new_output'] = {
                    'total': growth,
                    'segments': earth_segments,
                }
                check(
                    'earth_new_output',
                    growth['passed'] and all(s['passed'] for s in earth_segments),
                    'Collected earth products must be newly converted in every measurement segment, not depleted pre-converted blocks',
                )
            if task.get('material_growth'):
                growth = material_growth_balance(
                    task['material_growth'],
                    start_region,
                    final_region,
                    sum(
                        m['net_new_collected'] for m in report['metrics']['production'].values()
                    ),
                    item_stock,
                    task['id'] == 'hard.granular_materials.v1',
                )
                report['metrics']['material_growth'] = {
                    'total': growth,
                    'segments': material_segments,
                }
                check(
                    'material_net_growth',
                    growth['passed'] and all(s['passed'] for s in material_segments),
                    'Material inventory, placed blocks and falling blocks must grow by at least the collected output in every measurement segment',
                )
            if task['id'] == 'medium.piglin_bartering.v1':
                ledger = barter_balance(
                    start_region, final_region, report['metrics']['production'], item_stock
                )
                report['metrics']['barter_balance'] = ledger
                check(
                    'resource_balance',
                    ledger['passed'],
                    'Consumed gold must cover the minimum number of vanilla barter rolls implied by newly collected outputs; random ratios are not fixed',
                )
                check(
                    'item_components',
                    True,
                    'Barter equipment and potions match the running vanilla barter product components',
                )
            if variants:
                report['metrics']['variant_production'] = production_metrics(
                    variant_task, variant_start, variant_end, variant_debt
                )
                report['metrics']['variant_segments'] = variant_segments
                check(
                    'classified_storage',
                    True,
                    'Every item variant has separate physical storage and matching native components',
                )
                check(
                    'variant_output',
                    all(
                        m['threshold_passed']
                        for m in report['metrics']['variant_production'].values()
                    )
                    and all(
                        m['threshold_passed']
                        for segment in variant_segments
                        for m in segment.values()
                    ),
                    'Every required variant must meet its own new-output and sustained-rate targets',
                )
            aggregate_metrics = aggregate_output_metrics(task, report['metrics']['production'])
            if aggregate_metrics:
                report['metrics']['aggregate_output'] = aggregate_metrics
                report['metrics']['aggregate_segments'] = aggregate_segments
                check(
                    'aggregate_output',
                    all(g['threshold_passed'] for g in aggregate_metrics.values())
                    and all(
                        g['threshold_passed']
                        for segment in aggregate_segments
                        for g in segment.values()
                    ),
                    'Combined output targets must hold for the full window and every measurement segment',
                )
            report['metrics']['loose_items_after'] = final_region['loose_item_counts']
            report['metrics']['consumables'] = {
                'before': start_region['inventory_counts'],
                'after': final_region['inventory_counts'],
                'declared': op['consumables'],
            }
            needed_by_product = {
                p: max(
                    max(
                        (
                            s['products'][p]['max_stack']
                            for s in end['slots'].values()
                            if p in s['products']
                        ),
                        default=64,
                    )
                    * task['storage']['min_free_stack_slots'],
                    math.ceil(
                        report['metrics']['production'][p]['per_game_hour']
                        * task['storage']['headroom_ticks']
                        / 72000
                    ),
                )
                for p in products
            }
            final_capacity = allocate_storage(
                end['slots'],
                end['eligible'],
                task['storage']['reserved_stack_slots_per_primary'],
                headroom=needed_by_product,
            )
            report['metrics']['storage_final'] = final_capacity
            room_ok = final_capacity['passed']
            if room_ok:
                for product, storage in final_capacity['products'].items():
                    rate = report['metrics']['production'][product]['per_game_hour']
                    needed = max(
                        storage['max_stack'] * task['storage']['min_free_stack_slots'],
                        math.ceil(rate * task['storage']['headroom_ticks'] / 72000),
                    )
                    storage['required_free_items'] = needed
                    room_ok = room_ok and storage['free_items'] >= needed
            check(
                'storage_headroom',
                room_ok,
                'Free storage must support the fixed game-tick buffer at observed game-tick rate, with at least one free stack',
            )
            check(
                'measurement_window',
                all(
                    v['elapsed_game_ticks'] >= task['measurement']['window_ticks']
                    for v in report['metrics']['production'].values()
                ),
                'Rates use actual game ticks and server monotonic time',
            )
            if task['mode'] != 'open_efficiency':
                check(
                    'production_threshold',
                    all(
                        v['threshold_passed'] for v in report['metrics']['production'].values()
                    ),
                    'Every primary product must satisfy immutable minimum net count and game-hour rate',
                )
            else:
                report['checks'].append(
                    {
                        'id': 'production_threshold',
                        'status': 'not_applicable',
                        'message': 'Open efficiency task has no pass threshold; zero is a valid measured rate',
                        'evidence': [],
                    }
                )
            if self.reviewer:
                try:
                    report['ai_review'] = self.reviewer.review(
                        task, submission, evidence, report
                    )
                    by_id = {c['id']: c for c in report['ai_review'].get('checks', [])}
                    for name in reviews:
                        item = by_id.get(name)
                        report['checks'].append(
                            {
                                'id': name,
                                'status': item['verdict'] if item else 'inconclusive',
                                'message': (
                                    item['reason']
                                    if item
                                    else 'Required structural review omitted'
                                ),
                                'evidence': item.get('evidence_ids', []) if item else [],
                            }
                        )
                    report['checks'].append(
                        {
                            'id': 'ai_evidence_review',
                            'status': report['ai_review']['verdict'],
                            'message': 'AI review is additional evidence and cannot override deterministic failures',
                            'evidence': [],
                        }
                    )
                except Exception as exc:
                    report['checks'].append(
                        {
                            'id': 'ai_evidence_review',
                            'status': 'inconclusive',
                            'message': str(exc),
                            'evidence': [],
                        }
                    )
                    report['infrastructure'] = {
                        'status': 'review_unavailable',
                        'stage': 'review',
                        'recovered': False,
                        'fault': fault(exc),
                    }
        except (ValueError, AssertionError) as exc:
            check('acceptance_error', False, str(exc))
        except Exception as exc:
            report['metrics']['infrastructure_failure'] = {
                'type': type(exc).__name__,
                'message': str(exc),
            }
            report['checks'].append(
                {
                    'id': 'measurement_unavailable',
                    'status': 'inconclusive',
                    'message': str(exc),
                    'evidence': [],
                }
            )
        finally:
            if actor_guard is not None:
                if actor_guard.status == 'pass':
                    try:
                        actor_guard.sample()
                    except Exception as exc:
                        if actor_guard.status != 'fail':
                            report['metrics'].setdefault('infrastructure_failure', fault(exc))
                try:
                    report['metrics']['evaluator_actor'] = actor_guard.summary()
                    report['checks'].append(
                        {
                            'id': 'evaluator_actor_continuity',
                            'status': actor_guard.status,
                            'message': actor_guard.message,
                            'evidence': ['evidence.json#/observations'],
                        }
                    )
                except Exception as exc:
                    report['metrics'].setdefault('infrastructure_failure', fault(exc))
                    report['checks'].append(
                        {
                            'id': 'evaluator_actor_continuity',
                            'status': 'inconclusive',
                            'message': str(exc),
                            'evidence': [],
                        }
                    )
            try:
                report['cleanup_errors'] = self.session.cleanup()
            except Exception as exc:
                report['cleanup_errors'] = [str(exc)]
                report['metrics'].setdefault('infrastructure_failure', fault(exc))
            relevant = [c for c in report['checks'] if c['status'] != 'not_applicable']
            report['check_pass_fraction'] = (
                sum(c['status'] == 'pass' for c in relevant) / len(relevant) if relevant else 0
            )
            if report['metrics'].get('infrastructure_failure'):
                report['verdict'] = 'inconclusive'
            elif any(c['status'] == 'fail' for c in relevant):
                report['verdict'] = 'fail'
            elif (
                any(c['status'] == 'inconclusive' for c in relevant)
                or report['cleanup_errors']
                or 'production' not in report['metrics']
            ):
                report['verdict'] = 'inconclusive'
            else:
                report['verdict'] = 'measured' if task['mode'] == 'open_efficiency' else 'pass'
            report['goal_passed'] = (
                None
                if task['mode'] == 'open_efficiency' or report['verdict'] == 'inconclusive'
                else report['verdict'] == 'pass'
            )
            normalized = canonical_report(report)
            report.clear()
            report.update(normalized)
            write_measurement(directory, evidence, report)
        return report
