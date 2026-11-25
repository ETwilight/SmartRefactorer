"""Match required item components and count variants in distinct physical storage slots."""

import math


def components_match(actual, expected):
    """Check that actual components contain the expected key/value subtree, allowing
    additional actual fields.
    """

    return all(
        key in actual
        and (
            components_match(actual[key], value)
            if isinstance(value, dict) and isinstance(actual[key], dict)
            else actual[key] == value
        )
        for key, value in expected.items()
    )


def physical_slots(snapshot):
    """Return top-level inventory slots followed by their reported shulker-box child slots."""

    if snapshot.get('item_components_version') != 'item-components-v1':
        raise RuntimeError('Item component telemetry unavailable; update the server bridge')
    return snapshot['slots'] + [
        child for slot in snapshot['slots'] for child in slot.get('shulker_slots', [])
    ]


def classified_counts(targets, declared, snapshots, ref_key):
    """Count matching variant items while assigning each declared physical slot to one
    variant.
    """
    # counts: dict[str, int], keyed by variant ID rather than base item ID.

    counts = {}
    # owners: dict[str, str], mapping physical slot IDs to the variant owning their declared
    # storage.
    owners = {}
    for target in targets:
        variant = target['id']
        seen = set()
        counts[variant] = 0
        for ref in declared[variant]:
            for slot in physical_slots(snapshots[ref_key(ref)]):
                key = slot['slot_id']
                if key in owners and owners[key] != variant:
                    raise ValueError('Variant storage overlaps a physical container')
                owners[key] = variant
                if key in seen:
                    continue
                seen.add(key)
                if slot['count'] and slot['item'] == target['item']:
                    if not components_match(
                        slot.get('item_components', {}), target['components']
                    ):
                        raise ValueError(
                            'Incorrect item variant in classified storage: ' + variant
                        )
                    counts[variant] += slot['count']
    return counts


def variant_stock(targets, region):
    """Count each required variant in the region's item-component stock rows."""

    if 'component_item_counts' not in region:
        raise RuntimeError('Component stock telemetry unavailable; update the server bridge')
    return {
        t['id']: sum(
            r['count']
            for r in region['component_item_counts']
            if r['item'] == t['item'] and components_match(r['components'], t['components'])
        )
        for t in targets
    }


def validate_barter_components(snapshots, products):
    """Check enchantment and potion components of the selected barter products in output
    storage.
    """

    for snapshot in snapshots.values():
        for slot in physical_slots(snapshot):
            if not slot['count'] or slot['item'] not in products:
                continue
            c = slot.get('item_components', {})
            item = slot['item']
            if item in ('minecraft:enchanted_book', 'minecraft:iron_boots'):
                key = (
                    'minecraft:stored_enchantments'
                    if item == 'minecraft:enchanted_book'
                    else 'minecraft:enchantments'
                )
                levels = c.get(key, {})
                if set(levels) != {'minecraft:soul_speed'} or levels[
                    'minecraft:soul_speed'
                ] not in (1, 2, 3):
                    raise ValueError(
                        'Barter equipment must contain a valid Soul Speed enchantment'
                    )
            if item in ('minecraft:potion', 'minecraft:splash_potion'):
                potion = c.get('minecraft:potion_contents', {})
                allowed = (
                    {'minecraft:fire_resistance', 'minecraft:water'}
                    if item == 'minecraft:potion'
                    else {'minecraft:fire_resistance'}
                )
                if (
                    potion.get('potion') not in allowed
                    or potion.get('custom_effect_count') != 0
                ):
                    raise ValueError('Potion components do not match vanilla barter output')


def barter_balance(start, end, production, item_stock):
    """Compute minimum barter rolls from per-product output caps and compare them with
    observed gold-ingot consumption.
    """
    # Each credited product implies at least ceil(count / per-roll maximum) barter rolls.
    # Compare the sum of those roll counts with the observed gold-ingot stock decrease.

    maximum = {
        'minecraft:quartz': 12,
        'minecraft:gravel': 16,
        'minecraft:blackstone': 16,
        'minecraft:soul_sand': 8,
        'minecraft:obsidian': 1,
        'minecraft:crying_obsidian': 3,
        'minecraft:nether_brick': 8,
        'minecraft:string': 9,
        'minecraft:leather': 4,
        'minecraft:iron_nugget': 36,
        'minecraft:fire_charge': 1,
        'minecraft:iron_boots': 1,
        'minecraft:enchanted_book': 1,
        'minecraft:potion': 1,
        'minecraft:splash_potion': 1,
    }
    consumed = item_stock(start, 'minecraft:gold_ingot') - item_stock(
        end, 'minecraft:gold_ingot'
    )
    minimum_rolls = sum(
        math.ceil(m['net_new_collected'] / maximum[p]) for p, m in production.items()
    )
    return {
        'gold_consumed': consumed,
        'minimum_selected_output_rolls': minimum_rolls,
        'passed': consumed > 0 and consumed >= minimum_rolls,
    }
