"""Conservation of actual vanilla cooking events, fuel and in-flight inventory."""

if __package__:
    from environments.techmc.acceptance.harvest import source_drop_delta
else:
    from acceptance.harvest import source_drop_delta


def cold_start(region, feedstocks, item_stock):
    fuels = set(region.get('fuel_item_ids', []))
    if 'furnace_states' not in region or 'fuel_item_ids' not in region:
        raise RuntimeError('Fuel-state telemetry unavailable')
    nonempty = [
        s for s in region['furnace_states'] if s['fuel_count'] or s['lit_time_remaining']
    ]
    forbidden = {
        item: item_stock(region, item)
        for item in fuels - set(feedstocks)
        if item_stock(region, item)
    }
    return {
        'passed': not nonempty and not forbidden,
        'preloaded_furnaces': nonempty,
        'preloaded_fuel_items': forbidden,
        'raw_feedstock_exceptions': feedstocks,
    }


def cooking_balance(start, end, item_stock, source_drops=None):
    if not start.get('cooking_epoch') or start.get('cooking_epoch') != end.get('cooking_epoch'):
        raise RuntimeError('Cooking ledger restarted or is unavailable')
    delta = {}
    for field in ('consumed', 'produced', 'fuel'):
        a = start['cooking_totals'][field]
        b = end['cooking_totals'][field]
        delta[field] = {item: b.get(item, 0) - a.get(item, 0) for item in set(a) | set(b)}
        if any(n < 0 for n in delta[field].values()):
            raise RuntimeError('Cooking ledger counters decreased')
    if source_drops is None:
        source_drops = source_drop_delta(start, end)
    if any(not isinstance(n, int) or n < 0 for n in source_drops.values()):
        raise RuntimeError('Invalid natural input-generation adjustment')
    items = {
        item for field in ('consumed', 'produced') for item, n in delta[field].items() if n
    }
    items.update(start.get('processing_inputs', []))
    losses = {
        item: item_stock(start, item)
        + delta['produced'].get(item, 0)
        + source_drops.get(item, 0)
        - delta['consumed'].get(item, 0)
        - delta['fuel'].get(item, 0)
        - item_stock(end, item)
        for item in items
    }
    return {
        'passed': bool(delta['consumed'])
        and any(delta['consumed'].values())
        and all(n == 0 for n in losses.values()),
        'events': delta,
        'observed_source_drops': {
            i: source_drops.get(i, 0) for i in items if source_drops.get(i, 0)
        },
        'unaccounted_items': {i: n for i, n in losses.items() if n},
    }


def autonomous_fuel(start, end, feedstocks):
    delta = {
        item: end['cooking_totals']['fuel'].get(item, 0)
        - start['cooking_totals']['fuel'].get(item, 0)
        for item in set(start['cooking_totals']['fuel']) | set(end['cooking_totals']['fuel'])
    }
    direct_raw = {item: n for item, n in delta.items() if item in feedstocks and n > 0}
    return {
        'passed': sum(delta.values()) > 0 and not direct_raw,
        'consumed_after_warmup': delta,
        'raw_feedstock_burned_after_warmup': direct_raw,
    }


def conversion_balance(conversion, start, end, item_stock):
    def total(region, item):
        if 'inflight_block_counts' not in region:
            raise RuntimeError('Falling-block stock telemetry unavailable')
        return (
            item_stock(region, item)
            + region['block_counts'].get(item, 0)
            + region['inflight_block_counts'].get(item, 0)
        )

    used = total(start, conversion['input']) - total(end, conversion['input'])
    made = total(end, conversion['output']) - total(start, conversion['output'])
    return {
        'input_consumed': used,
        'output_created': made,
        'passed': used > 0
        and made > 0
        and used * conversion['output_units'] == made * conversion['input_units'],
    }


def wither_recipe_balance(start, end, item_stock):
    def stocks(r, items):
        return sum(item_stock(r, i) + r['block_counts'].get(i, 0) for i in items)

    sand = ('minecraft:soul_sand', 'minecraft:soul_soil')
    skulls = ('minecraft:wither_skeleton_skull', 'minecraft:wither_skeleton_wall_skull')
    sand_used = stocks(start, sand) - stocks(end, sand)
    skulls_used = stocks(start, skulls) - stocks(end, skulls)
    units = (
        item_stock(end, 'minecraft:nether_star')
        - item_stock(start, 'minecraft:nether_star')
        + end['entity_counts'].get('minecraft:wither', 0)
        - start['entity_counts'].get('minecraft:wither', 0)
    )
    return {
        'summoned_units': units,
        'soul_material_consumed': sand_used,
        'skulls_consumed': skulls_used,
        'passed': units > 0 and sand_used == units * 4 and skulls_used == units * 3,
    }


def material_growth_balance(items, start, end, new_output, item_stock, renew_inputs=False):
    def inventory(r, item):
        return item_stock(r, item) + r.get('operating_player_counts', {}).get(item, 0)

    def material(r, item):
        return (
            inventory(r, item)
            + r['block_counts'].get(item, 0)
            + r['inflight_block_counts'].get(item, 0)
        )

    def total(r):
        if 'inflight_block_counts' not in r:
            raise RuntimeError('Falling-block stock telemetry unavailable')
        return sum(material(r, i) for i in items)

    growth = total(end) - total(start)
    depleted = {}
    shells = None
    if renew_inputs:
        initial_items = set(start.get('total_item_counts', {})) | set(
            start.get('operating_player_counts', {})
        )
        depleted = {
            i: inventory(start, i) - inventory(end, i)
            for i in initial_items
            if i not in items
            and not i.endswith('shulker_box')
            and inventory(end, i) < inventory(start, i)
        }
        shell_items = {
            i
            for r in (start, end)
            for field in ('total_item_counts', 'block_counts', 'operating_player_counts')
            for i in r.get(field, {})
            if i.endswith('shulker_box')
        }
        shells = {
            'before': sum(material(start, i) for i in shell_items),
            'after': sum(material(end, i) for i in shell_items),
        }
        # Supplied box bodies are packaging consumables. Their stock is
        # diagnostic only; the task never requires an upstream shell farm.
    return {
        'material_net_growth': growth,
        'new_collected_output': new_output,
        'depleted_operating_inputs': depleted,
        'shulker_shell_stock': shells,
        'passed': new_output > 0 and growth >= new_output and not depleted,
    }


def world_output_balance(products, start, end, collected, item_stock):
    """A preplaced block reservoir cannot be credited as new renewable output."""
    block_aliases = {
        'minecraft:weeping_vines': ('minecraft:weeping_vines', 'minecraft:weeping_vines_plant'),
        'minecraft:twisting_vines': (
            'minecraft:twisting_vines',
            'minecraft:twisting_vines_plant',
        ),
    }

    def total(r, item):
        if 'inflight_block_counts' not in r:
            raise RuntimeError('In-flight block stock telemetry unavailable')
        return (
            item_stock(r, item)
            + r.get('operating_player_counts', {}).get(item, 0)
            + sum(
                r['block_counts'].get(block, 0) + r['inflight_block_counts'].get(block, 0)
                for block in block_aliases.get(item, (item,))
            )
        )

    measurements = {}
    for item in products:
        before = total(start, item)
        after = total(end, item)
        credited = collected.get(item, 0)
        measurements[item] = {
            'before': before,
            'after': after,
            'net_growth': after - before,
            'new_collected': credited,
            'passed': credited > 0 and after - before >= credited,
        }
    return {
        'passed': bool(measurements) and all(m['passed'] for m in measurements.values()),
        'products': measurements,
    }
