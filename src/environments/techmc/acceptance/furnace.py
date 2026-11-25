"""Check complete-machine bounds and sustained recipe throughput for every installed furnace."""


def validate_machine_limits(task, submission):
    """Check the common machine envelope against axis and subchunk limits, returning its
    bounds summary when configured.
    """

    limits = task.get('machine_limits')
    if not limits:
        return
    regions = [
        {'dimension': submission['dimension'], 'bounds': submission['bounds']}
    ] + submission.get('regions', [])
    if any(r['dimension'] != submission['dimension'] for r in regions):
        raise ValueError('The complete machine must fit in one dimension')
    minimum = [min(r['bounds']['min'][axis] for r in regions) for axis in range(3)]
    maximum = [max(r['bounds']['max'][axis] for r in regions) for axis in range(3)]
    size = [b - a + 1 for a, b in zip(minimum, maximum)]
    if 'max_size' in limits:
        # oversized: list[int] of axis indices exceeding their normal size limit.
        oversized = [
            axis for axis, (n, limit) in enumerate(zip(size, limits['max_size'])) if n > limit
        ]
        extension = limits.get('one_axis_max_size')
        if oversized and (
            extension is None or len(oversized) > 1 or size[oversized[0]] > extension
        ):
            raise ValueError(
                'The complete machine, collection, packing and storage must fit within the published bounding-box dimensions; any declared extension applies to at most one axis'
            )
    # Count aligned subchunks touched by inclusive bounds, including cells on both sides of a
    # boundary.
    spans = [b // 16 - a // 16 + 1 for a, b in zip(minimum, maximum)]
    if 'max_subchunks_per_axis' in limits and any(
        n > limits['max_subchunks_per_axis'] for n in spans
    ):
        raise ValueError(
            'The complete machine, input, fuel system and output must fit within the published subchunk bounds'
        )
    return {
        'dimension': submission['dimension'],
        'bounds': {'min': minimum, 'max': maximum},
        'size': size,
        'subchunks_per_axis': spans,
        'limits': dict(limits),
    }


def furnace_activity(limits, start, end, ticks, product):
    """Compare installed furnaces and native recipe counters across a segment to check each
    furnace's production rate.
    """

    if ticks <= 0:
        raise RuntimeError('Invalid furnace measurement duration')
    if not start.get('cooking_epoch') or start['cooking_epoch'] != end.get('cooking_epoch'):
        raise RuntimeError('Per-furnace recipe ledger is unavailable or restarted')
    if 'cooking_nodes' not in start or 'cooking_nodes' not in end:
        raise RuntimeError('Per-furnace recipe telemetry unavailable')

    def furnaces(region):
        """Return IDs of installed furnace containers in this region snapshot."""

        return {c['id'] for c in region['containers'] if c['type'] == 'minecraft:furnace'}

    before = furnaces(start)
    after = furnaces(end)

    # Use dimension/position keys to join ledger nodes independently of container-ID formatting.
    def key(node):
        """Build a ledger-node key from its dimension and block-position tuple."""

        return (node['dimension'], tuple(node['position']))

    def stock(region):
        """Map native furnace-node positions to cumulative counts of the measured recipe
        output.
        """

        return {key(n): n['produced'].get(product, 0) for n in region['cooking_nodes']}

    # old and new: dict[tuple[str, tuple[int, int, int]], int] of cumulative product counts.
    old = stock(start)
    new = stock(end)
    positions = {
        tuple(c['position']) for c in end['containers'] if c['type'] == 'minecraft:furnace'
    }
    # rates: dict[str, float], using stringified ledger keys for JSON report fields.
    rates = {
        str(k): (new.get(k, 0) - old.get(k, 0)) * 72000 / ticks
        for k in set(old) | set(new)
        if k[1] in positions
    }
    if any(v < 0 for v in rates.values()):
        raise RuntimeError('Per-furnace recipe counters decreased')
    count = limits['furnaces']
    minimum = limits['min_items_per_furnace_per_game_hour']
    active = sum(v >= minimum for v in rates.values())
    return {
        'passed': before == after and len(after) == count and active == count,
        'installed_furnaces': len(after),
        'sustained_active_furnaces': active,
        'required_furnaces': count,
        'minimum_per_furnace_per_game_hour': minimum,
        'per_furnace_rates': rates,
    }
