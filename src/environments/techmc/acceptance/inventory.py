"""Fixed inventory batch acceptance, independent of machine layout."""


def measure_bucket_batch(
    task, product, actor, call, region, check, report, tick_mode, sprint_wait, natural_wait
):
    initial = call('fake_player.bucket_batch', {'id': actor, 'product': product})
    if (
        initial.get('version') != 'fake-inventory-v1'
        or initial.get('batch', {}).get('version') != 'bucket-batch-v1'
    ):
        raise RuntimeError('Bucket batch telemetry unavailable')
    slots = [s for s in initial['slots'] if s['index'] < 36]
    if len(slots) != 36 or any(
        s['item'] != 'minecraft:bucket' or s['count'] != 1 for s in slots
    ):
        raise ValueError('Bucket batch must begin with exactly 36 single empty buckets')
    start = call('clock.query')['tick']
    limit = task['measurement']['window_ticks']
    state = initial
    while True:
        details = state.get('batch', {})
        if details.get('version') != 'bucket-batch-v1':
            raise RuntimeError('Bucket batch telemetry unavailable')
        if details.get('error'):
            raise ValueError(details['error'])
        elapsed = call('clock.query')['tick'] - start
        if details.get('complete') or elapsed >= limit:
            break
        ticks = min(200, limit - elapsed)
        wait = (
            sprint_wait(call, ticks, 120) if tick_mode == 'sprint' else natural_wait(ticks, 120)
        )
        if wait['status'] != 'completed':
            raise RuntimeError(wait)
        state = call('fake_player.inventory', {'id': actor})
    call('fake_player.stop', {'id': actor})
    final = call('fake_player.inventory', {'id': actor})
    region()
    slots = [s for s in final['slots'] if s['index'] < 36]
    passed = len(slots) == 36 and all(s['item'] == product and s['count'] == 1 for s in slots)
    check(
        'bucket_batch',
        passed,
        'All 36 original empty buckets must become the selected product in the evaluator inventory',
    )
    check(
        'no_extra_fake_players_final',
        not any(p.get('fake') and p['name'] != actor for p in call('observe.actors')),
        'No external fake players at batch completion',
    )
    report['metrics']['inventory_batch'] = {
        'initial': initial,
        'final': final,
        'elapsed_game_ticks': elapsed,
        'limit_ticks': limit,
        'replenished': False,
    }
    report['metrics']['production'] = {
        product: {
            'net_new_collected': sum(s['count'] for s in slots if s['item'] == product),
            'threshold_passed': passed,
        }
    }
