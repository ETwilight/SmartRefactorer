"""Actual vanilla wither-source and natural-bedrock death provenance."""

FIELDS = (
    'spawned',
    'spawned_near_bedrock',
    'deaths',
    'deaths_near_bedrock',
    'untracked_deaths',
)


def collect_wither_sources(readbacks):
    if not readbacks:
        raise RuntimeError('Wither-source telemetry unavailable')
    epoch = None
    radius = None
    totals = {k: 0 for k in FIELDS}
    events = set()
    for value in readbacks:
        if (
            value.get('version') != 'wither-sources-v1'
            or value.get('complete') is not True
            or value.get('natural_world') is not True
        ):
            raise RuntimeError('Complete natural-world wither-source telemetry unavailable')
        if not value.get('epoch') or value.get('radius') != 8:
            raise RuntimeError('Wither-source identity or proximity contract unavailable')
        if epoch is None:
            epoch = value['epoch']
            radius = value['radius']
        if epoch != value['epoch'] or radius != value['radius']:
            raise RuntimeError('Wither-source ledger restarted during snapshot')
        for field in FIELDS:
            n = value.get(field)
            if not isinstance(n, int) or n < 0:
                raise RuntimeError('Invalid wither-source counter')
            totals[field] += n
        if (
            value['spawned_near_bedrock'] > value['spawned']
            or value['deaths_near_bedrock'] > value['deaths']
            or value['untracked_deaths'] > value['deaths']
        ):
            raise RuntimeError('Inconsistent wither-source proximity counters')
        local = {k: 0 for k in FIELDS}
        for event in value.get('events', []):
            key = (event['kind'], event['entity_uuid'])
            if key in events:
                raise ValueError(
                    'Overlapping wither-source events would double-count the same entity'
                )
            events.add(key)
            if event['kind'] == 'spawn':
                local['spawned'] += 1
                local['spawned_near_bedrock'] += bool(event['near_bedrock'])
            elif event['kind'] == 'star_death':
                local['deaths'] += 1
                local['deaths_near_bedrock'] += bool(event['near_bedrock'])
                local['untracked_deaths'] += not event['natural_spawn']
            else:
                raise RuntimeError('Unknown wither-source event')
        if any(local[k] != value[k] for k in FIELDS):
            raise RuntimeError('Wither-source counters do not match the actual entity events')
    return {
        'wither_source_epoch': epoch,
        'wither_source_radius': radius,
        'wither_source_complete': True,
        'wither_sources': totals,
    }


def natural_wither_output(start, end, credited_stars):
    if (
        not start.get('wither_source_complete')
        or not end.get('wither_source_complete')
        or not start.get('wither_source_epoch')
        or start['wither_source_epoch'] != end.get('wither_source_epoch')
    ):
        raise RuntimeError('Wither-source ledger unavailable or restarted')
    delta = {k: end['wither_sources'][k] - start['wither_sources'][k] for k in FIELDS}
    if any(n < 0 for n in delta.values()):
        raise RuntimeError('Wither-source counters decreased')
    return {
        'passed': credited_stars > 0
        and delta['untracked_deaths'] == 0
        and delta['deaths'] > 0
        and delta['deaths_near_bedrock'] == delta['deaths']
        and delta['deaths_near_bedrock'] >= credited_stars,
        'credited_new_stars': credited_stars,
        'measurement_events': delta,
        'natural_bedrock_radius': end['wither_source_radius'],
    }
