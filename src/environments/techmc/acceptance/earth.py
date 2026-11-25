"""Account for earth-material quantities, recipe transactions, and operating supplies.

EARTH_UNITS measures material in quarter-block units: a clay block is four
units and a clay ball is one. Stock includes inventories, operating-player
holdings, placed blocks, and falling blocks. Recipe events account for
changes in these family totals caused by the actual crafting transactions.
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
    """Add the operating player's holdings to the supplied region inventory/loose-item count."""
    return item_stock(region, item) + region.get('operating_player_counts', {}).get(item, 0)


def material_stock(region, item, item_stock):
    """Count one material in inventories, the operating player, placed blocks, and falling
    blocks.
    """

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
    """Reconcile earth-family stock changes with recipe-event adjustments and auxiliary-input
    consumption.
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
    # totals: dict[str, dict[str, int]], containing consumed, produced, and fuel deltas by item
    # ID.
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
    # Recipe events can change the total quarter-block units represented by these material
    # families.
    # Apply their consumed/produced/fuel adjustment before checking the remaining stock
    # difference.
    recipe_adjustment = sum(
        EARTH_UNITS.get(i, 0) * n for i, n in totals['produced'].items()
    ) - sum(
        EARTH_UNITS.get(i, 0) * n
        for field in ('consumed', 'fuel')
        for i, n in totals[field].items()
    )
    ledger_inputs = {}
    # auxiliary_losses: dict[str, int | str], holding stock differences or a
    # missing-required-craft message.
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
    # All terms use quarter-block units, including the recipe transaction adjustment.
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
    """Compare credited collected products with growth in their complete
    inventory/block/falling-block stocks.
    """
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
    """Check initial filled-potion stock when requested and conservation of bottle items
    across operation.
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
    """Return the permitted single-entry feedstock item IDs for an earth-material profile."""
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
