"""Actual block-loot accounting and exact initial-amethyst output debt."""


def collect_harvest(readbacks):
    """Combine non-overlapping regional readbacks from one atomic snapshot."""
    if not readbacks:
        raise RuntimeError('Harvest telemetry unavailable')
    epoch = None
    sequence = None
    source_drops = {}
    events = []
    nodes = set()
    seen_events = set()
    for value in readbacks:
        if value.get('version') != 'harvest-ledger-v1' or value.get('complete') is not True:
            raise RuntimeError('Complete vanilla block-drop telemetry unavailable')
        if not value.get('epoch') or not isinstance(value.get('sequence'), int):
            raise RuntimeError('Harvest identity telemetry unavailable')
        if epoch is None:
            epoch = value['epoch']
            sequence = value['sequence']
        if epoch != value['epoch'] or sequence != value['sequence']:
            raise RuntimeError('Harvest readbacks are not from one atomic snapshot')
        for node in value.get('drop_nodes', []):
            key = (node['dimension'], tuple(node['position']))
            if key in nodes:
                raise ValueError('Overlapping block-drop readbacks would double-count material')
            nodes.add(key)
            for item, count in node['drops'].items():
                if not isinstance(count, int) or count < 0:
                    raise RuntimeError('Invalid cumulative block-drop count')
                source_drops[item] = source_drops.get(item, 0) + count
        for event in value.get('amethyst_events', []):
            key = (event['dimension'], event['harvest_sequence'])
            if key in seen_events:
                raise ValueError('Duplicate amethyst harvest event')
            seen_events.add(key)
            if (
                event['count'] <= 0
                or event['harvest_sequence'] > sequence
                or event['matured_sequence'] > event['harvest_sequence']
                or event['matured_tick'] > event['harvest_tick']
            ):
                raise RuntimeError('Invalid amethyst event chronology')
            events.append(event)
    return {
        'harvest_epoch': epoch,
        'harvest_sequence': sequence,
        'harvest_complete': True,
        'source_drops': source_drops,
        'amethyst_events': events,
    }


def source_drop_delta(start, end):
    """Do not silently treat unavailable natural input generation as zero."""
    if (
        not start.get('harvest_complete')
        or not end.get('harvest_complete')
        or not start.get('harvest_epoch')
        or start['harvest_epoch'] != end.get('harvest_epoch')
    ):
        raise RuntimeError('Natural input-generation telemetry is unavailable or restarted')
    if end['harvest_sequence'] < start['harvest_sequence']:
        raise RuntimeError('Harvest sequence decreased')
    result = {
        item: end['source_drops'].get(item, 0) - start['source_drops'].get(item, 0)
        for item in set(start['source_drops']) | set(end['source_drops'])
    }
    if any(n < 0 for n in result.values()):
        raise RuntimeError('Cumulative block-drop counters decreased')
    return result


def amethyst_ineligible_drops(start_tick, previous, current, *, start_sequence=None):
    """Return exact shard drops from clusters already mature at measure start.

    The sequence boundary is preferable to a tick boundary: a cluster can mature
    after an atomic snapshot but during the same game tick. Actual returned loot
    determines count, including Fortune, explosions, and empty/silk-touch loot.
    """
    source_drop_delta(previous, current)
    old_sequence = previous['harvest_sequence']
    new_sequence = current['harvest_sequence']
    ineligible = []
    eligible = []
    for event in current.get('amethyst_events', []):
        if not old_sequence < event['harvest_sequence'] <= new_sequence:
            continue
        if start_sequence is None:
            old = event['matured_tick'] <= start_tick
        else:
            if 'matured_sequence' not in event:
                raise RuntimeError('Precise amethyst maturation order unavailable')
            old = event['matured_sequence'] <= start_sequence
        (ineligible if old else eligible).append(event)
    return {
        'ineligible_count': sum(e['count'] for e in ineligible),
        'eligible_count': sum(e['count'] for e in eligible),
        'ineligible_events': ineligible,
        'eligible_events': eligible,
        'measurement_start_tick': start_tick,
        'measurement_start_sequence': start_sequence,
    }
