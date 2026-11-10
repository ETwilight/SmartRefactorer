"""Vanilla earth-material conservation and automatic operating-resource checks.

Material quantities use quarter-block units so a clay block and its four clay
balls are equal. Inventory includes packed boxes through item_stock; block and
falling-block stocks remain in the same ledger. No machine shape is prescribed.
"""

EARTH_UNITS = {
    'minecraft:dirt': 4,
    'minecraft:coarse_dirt': 4,
    'minecraft:rooted_dirt': 4,
    'minecraft:gravel': 4,
    'minecraft:grass_block': 4,
    'minecraft:podzol': 4,
    'minecraft:mycelium': 4,
    'minecraft:farmland': 4,
    'minecraft:dirt_path': 4,
    'minecraft:mud': 4,
    'minecraft:packed_mud': 4,
    'minecraft:mud_bricks': 4,
    'minecraft:clay': 4,
    'minecraft:clay_ball': 1,
    'minecraft:terracotta': 4,
    'minecraft:brick': 1,
    'minecraft:bricks': 4,
}
POTION_ITEMS = ('minecraft:potion', 'minecraft:splash_potion', 'minecraft:lingering_potion')
BOTTLE_ITEMS = ('minecraft:glass_bottle',) + POTION_ITEMS


def inventory_stock(region, item, item_stock):
    """Include the operating player's actual holdings, without creating stock."""
    return item_stock(region, item) + region.get('operating_player_counts', {}).get(item, 0)


def material_stock(region, item, item_stock):
    if 'inflight_block_counts' not in region:
        raise RuntimeError('Falling-block stock telemetry unavailable for earth accounting')
    return (
        inventory_stock(region, item, item_stock)
        + region['block_counts'].get(item, 0)
        + region['inflight_block_counts'].get(item, 0)
    )


def earth_mass_balance(
    start, end, item_stock, products=(), auxiliary_inputs=('minecraft:wheat',)
):
    """Account for conversions and real crafter inputs, not merely delivered items.

    Use the operation-start snapshots for conservation. Separately use
    earth_new_output on measurement-start/end snapshots to exclude pre-grown or
    pre-converted buffers from new production.
    """
    before = {item: material_stock(start, item, item_stock) for item in EARTH_UNITS}
    after = {item: material_stock(end, item, item_stock) for item in EARTH_UNITS}
    initial_units = sum(before[item] * units for item, units in EARTH_UNITS.items())
    final_units = sum(after[item] * units for item, units in EARTH_UNITS.items())
    if not start.get('cooking_epoch') or start.get('cooking_epoch') != end.get('cooking_epoch'):
        raise RuntimeError(
            'Recipe-event ledger restarted or is unavailable for earth accounting'
        )
    if 'crafting' not in start.get('recipe_observers', []) or 'crafting' not in end.get(
        'recipe_observers', []
    ):
        raise RuntimeError('Actual vanilla crafter-event telemetry unavailable')
    totals = {}
    for field in ('consumed', 'produced', 'fuel'):
        initial = start['cooking_totals'][field]
        final = end['cooking_totals'][field]
        totals[field] = {
            item: final.get(item, 0) - initial.get(item, 0)
            for item in set(initial) | set(final)
        }
        if any(n < 0 for n in totals[field].values()):
            raise RuntimeError('Recipe-event counters decreased')
    # Minecraft recipes need not conserve physical volume. Account for the
    # actual vanilla transaction before testing the remaining family balance.
    recipe_adjustment = sum(
        EARTH_UNITS.get(i, 0) * n for i, n in totals['produced'].items()
    ) - sum(
        EARTH_UNITS.get(i, 0) * n
        for field in ('consumed', 'fuel')
        for i, n in totals[field].items()
    )
    ledger_inputs = {}
    auxiliary_losses = {}
    for item in auxiliary_inputs:
        consumed = totals['consumed'].get(item, 0) + totals['fuel'].get(item, 0)
        produced = totals['produced'].get(item, 0)
        loss = (
            inventory_stock(start, item, item_stock)
            + produced
            - consumed
            - inventory_stock(end, item, item_stock)
        )
        ledger_inputs[item] = {'consumed': consumed, 'produced': produced, 'unaccounted': loss}
        if loss:
            auxiliary_losses[item] = loss
    if (
        'minecraft:packed_mud' in products
        and totals['produced'].get('minecraft:packed_mud', 0) <= 0
    ):
        auxiliary_losses['minecraft:packed_mud'] = 'No completed vanilla craft recorded'
    unaccounted = initial_units + recipe_adjustment - final_units
    return {
        'passed': unaccounted == 0 and not auxiliary_losses,
        'unit': 'quarter_earth_block',
        'initial_units': initial_units,
        'final_units': final_units,
        'vanilla_recipe_adjustment_units': recipe_adjustment,
        'unaccounted_units': unaccounted,
        'initial_materials': before,
        'final_materials': after,
        'auxiliary_inputs': ledger_inputs,
        'unaccounted_auxiliary_inputs': auxiliary_losses,
    }


def earth_new_output(start, end, collected, item_stock):
    """Pre-converted world blocks cannot become newly produced inventory credit."""
    created = {
        item: material_stock(end, item, item_stock) - material_stock(start, item, item_stock)
        for item in collected
    }
    return {
        'passed': bool(collected)
        and all(n > 0 and created[item] >= n for item, n in collected.items()),
        'new_material_by_product': created,
        'new_collected_by_product': dict(collected),
    }


def earth_operating_supplies(start, end, item_stock, *, check_startup=True):
    """Empty bottles may be supplied; prefilled potions cannot bypass filling.

    The shared single-input checker must include glass_bottle and all raw inputs.
    The operating-player policy must permit only the declared placement/tilling
    action, not water-bottle filling. With no human restocking, vanilla mud creation
    then requires the machine to supply water bottles automatically.
    """
    filled = {
        item: inventory_stock(start, item, item_stock)
        for item in POTION_ITEMS
        if inventory_stock(start, item, item_stock)
    }
    before = sum(inventory_stock(start, item, item_stock) for item in BOTTLE_ITEMS)
    after = sum(inventory_stock(end, item, item_stock) for item in BOTTLE_ITEMS)
    return {
        'passed': (not check_startup or not filled) and after >= before,
        'initial_filled_potions': filled,
        'initial_bottles': before,
        'final_bottles': after,
        'bottle_net_change': after - before,
        'startup_checked': check_startup,
    }


def earth_input_items(profile_id):
    """Inputs authorized at the single entry; selected outputs are not feedstock."""
    dirt = ['minecraft:dirt', 'minecraft:coarse_dirt', 'minecraft:rooted_dirt']
    if profile_id == 'dirt':
        return ['minecraft:coarse_dirt', 'minecraft:rooted_dirt']
    if profile_id == 'mud':
        return dirt + ['minecraft:glass_bottle']
    if profile_id in ('clay_ball', 'clay', 'terracotta'):
        return dirt + ['minecraft:mud', 'minecraft:glass_bottle']
    if profile_id == 'packed_mud':
        return dirt + ['minecraft:mud', 'minecraft:wheat', 'minecraft:glass_bottle']
    if profile_id == 'earth_factory':
        return dirt + ['minecraft:glass_bottle']
    raise ValueError('Unknown earth-material profile: ' + str(profile_id))
