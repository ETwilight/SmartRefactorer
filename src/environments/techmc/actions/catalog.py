"""Shared primitive catalogue for every external controller."""

KNOWLEDGE_METHODS = frozenset(
    {
        'wiki.search',
        'wiki.read',
        'wiki.provenance',
        'wiki.asset',
        'source.search',
        'source.read',
    }
)

TOOLS = {
    'observe.afk_site': 'server position/dimension; read-only body collision, support and fluid observations; no loading or world edits',
    'session.describe': 'side client/server',
    'observe.player': 'client player, inventory, GUI',
    'observe.blocks': 'min/max arrays, dimension; bounded 32768 blocks. Returns blocks with position, loaded and state. state is the native BlockState string (e.g. Block{minecraft:air} or Block{minecraft:lever}[face=wall,facing=north,powered=false]), not a registry ID; unloaded entries have loaded=false and state=unavailable. observe.region block_counts uses registry IDs for aggregate counts.',
    'observe.biome': 'server x/y/z/dimension; only loaded chunks, authoritative biome ID',
    'observe.entities': 'client radius <=128',
    'observe.block_entity': 'server x/y/z/dimension',
    'observe.player_state': 'client lightweight connection/identity/health/death/position/flight and actual connected game port; no inventory',
    'observe.actor_state': 'server bound actor identity/health/death/native deaths/connection serial and game port; no mutation',
    'action.look': 'client yaw/pitch or target array',
    'action.move': 'client target array, timeout_ms; straight walk, obstruction timeout',
    'action.interact': 'client x/y/z, face, optional hit array/hand; normal use/place',
    'action.break': 'client x/y/z/face/timeout_ms; normal player breaking',
    'action.respawn': 'client optional timeout_ms; vanilla respawn request only when dead; alive is a no-op; completion requires observed positive health',
    'action.cancel': 'client optional action_id',
    'action.status': 'client action_id',
    'inventory.transfer': 'client container_id/from/to/count',
    'command.describe': 'server empty params: permitted command index; command plus optional path of literal/argument node names: paginated real Brigadier usage; offset/limit (default 32, max 256)',
    'inventory.select': 'client hotbar slot 0..8',
    'command.run': 'server text + optional context executor/position/dimension/yaw/pitch; data get only, item replace only; command.describe returns branch policies; locate biome or structure <id-or-tag> with explicit context; no fillbiome/locate poi; zero-change fill retains raw failed callbacks and effect=no_change, which does not prove the requested state',
    'clock.query': 'server actual server tick/frozen/target_tps/sprinting',
    'experiment.conditions': 'server time/weather/gamerules/difficulty/player effects by dimension',
    'clock.freeze': 'server',
    'clock.unfreeze': 'server',
    'clock.step': 'server ticks',
    'clock.rate': 'server tps',
    'clock.sprint': 'server ticks',
    'counter.read': 'server wool color',
    'counter.reset': 'server wool color; consumes output items',
    'fake_player.spawn': 'server id/x/y/z/dimension/yaw/pitch; owned by this run',
    'fake_player.list': 'server owned fakes',
    'fake_player.status': 'server id',
    'fake_player.attack': 'server id/mode once|continuous|interval/interval_ticks',
    'fake_player.use': 'server id/mode/interval_ticks',
    'fake_player.look': 'server id/yaw/pitch',
    'fake_player.attack_and_use': 'server id/mode=continuous; one stationary actor attacks and drinks automatically delivered ominous bottles; never injects supplies',
    'fake_player.use_with_offhand': 'server id/item/mode=continuous; normal use action, moving only the declared material from the actor inventory to an empty offhand',
    'fake_player.use_material': 'server id/item/mode=continuous; use only the declared material delivered into the owned player inventory; no item injection',
    'fake_player.drop_inventory': 'server id; normally drop the owned player existing inventory at its current position without creating supplies',
    'machine.control': 'server x/y/z/dimension/action=set_lever|press_button; existing vanilla lever selection or button activation only',
    'fake_player.move': 'server id/direction forward|backward|left|right',
    'fake_player.stop': 'server id',
    'fake_player.remove': 'server id',
    'fake_player.inventory': 'server id; owned fake player inventory and active batch readback',
    'fake_player.equip': 'server id/item/components; replace the owned player mainhand with one survival-valid tool',
    'fake_player.bucket_batch': 'server id/product; initialize 36 empty buckets and use only empty buckets at the fixed target; no replenishment',
    'schematic.list': 'client files under project schematics/',
    'schematic.load': 'client file',
    'schematic.place': 'client schematic_id/x/y/z/rotation/mirror; PROJECTION ONLY',
    'schematic.materials': 'client placement_id, optional refresh',
    'schematic.diff': 'client placement_id',
    'schematic.save': 'client file/min/max; loaded region only',
    'mod.config_get': 'client key',
    'mod.config_set': 'client key/enabled; whitelist only',
    'debug.capabilities': 'client',
    'debug.snapshot': 'client typed core F3 data',
    'debug.entries.list': 'client actual vanilla providers',
    'debug.entries.read': 'client id',
    'debug.overlay.get': 'client',
    'debug.overlay.set': 'client enabled, optional entries map ID:ALWAYS_ON|IN_OVERLAY|NEVER',
    'debug.chunk_borders.get': 'client',
    'debug.chunk_borders.set': 'client enabled',
    'debug.chunk_info': 'client optional x/y/z',
    'debug.advanced_tooltips.get': 'client',
    'debug.advanced_tooltips.set': 'client enabled',
    'debug.item': 'client inventory slot',
    'debug.hitboxes.get': 'client',
    'debug.hitboxes.set': 'client enabled',
    'debug.entity_geometry': 'client radius',
    'debug.target': 'client',
    'debug.performance': 'client',
    'debug.capture': 'client screenshot scheduled',
    'client.capture': 'client screenshot scheduled',
    'client.gui_action': 'client exact screen class, operation close|click|scroll|text; containers/mod GUIs only',
    'wiki.search': 'local query/language=en/limit',
    'wiki.read': 'local entry/optional section/fields; default text and headings; explicitly request html/assets/links when needed; provenance always included',
    'wiki.provenance': 'local entry',
    'source.search': 'local query/side=client|server|mod:carpet/limit; mode=text (safe literal terms, default), phrase (exact phrase), fts (explicit SQLite FTS syntax); omit side to search all',
    'source.read': 'local path/start/count; source lines + SHA256',
    'experiment.wait': 'local passive server-clock polling: ticks/timeout_seconds/require_natural; never changes or accelerates the clock. Timeout, frozen or cancelled waits are completed calls with error status and the full wait result; they do not prove the requested ticks elapsed.',
    'experiment.measure': 'color/ticks/timeout_seconds/warmup_ticks; actual measurement',
}


LOCAL_DIAGNOSTICS = frozenset({'experiment.resources'})

TOOLS.update(
    {
        'observe.container': 'server x/y/z or entity_uuid, products. Direct backend native payload is at feedback.result.result; reference/summary views must be recalled for omitted fields. An unloaded block returns {loaded:false,position:[x,y,z],source:"server"}; this is unavailable evidence, not an empty inventory. A loaded result has loaded:true,dimension,container_id,members,locations,type,slots,slot_count,inventory_accounting_version,item_components_version,source,tick,sample_monotonic_ns. openable describes chest obstruction when applicable. Each slots entry has physical slot_id,index,item,count,products,accepts_shulker,item_components; shulker_slots is present for nested shulker contents. products maps each requested item ID to {max_stack,capacity,free}; no requested products means an empty products mapping, not zero capacity. A loaded non-container raises an error instead of returning an empty inventory.',
        'observe.performance': 'server recent MSPT statistics, target TPS and measured wall TPS between queries; read-only',
        'experiment.resources': 'local on-demand memory sample for the assigned experiment runner; no PID or path arguments',
        'observe.health': 'server JVM heap, processor count and per-dimension loaded chunk telemetry; read-only',
        'observe.containers': 'server containers/products/dimension; atomic batch inventory snapshot. Direct backend native payload is at feedback.result.result: {containers:[individual observe.container results in request order],tick,sample_monotonic_ns,source:"server"}. Each entry retains its loaded flag and physical slot IDs; unavailable entries must not be treated as empty containers. The slots and per-product capacity/free fields follow observe.container; this method does not return a flattened items or total_count mapping.',
        'observe.region': 'server min/max/dimension; inclusive ordered bounds, at most 32768 blocks per read; tile larger sites into bounded reads. Default basic summary; response.sections selects basic/equipment/cooking/sources/activity; response.detail=full retains all native fields.',
        'observe.actors': 'server loaded real/fake player positions',
        'experiment.restore': 'server keep list; restore this session temporary conditions',
        'trace.start': 'local optional categories',
        'trace.read': 'local after_sequence/limit',
        'trace.stop': 'local',
        'wiki.asset': 'local path; information-page assets, base64 encoded',
    }
)


def action_sides(method):
    if method in LOCAL_DIAGNOSTICS:
        return ['local']
    if method.startswith(('wiki.', 'source.', 'trace.')):
        return ['local']
    if method in ('session.describe', 'observe.blocks', 'observe.performance'):
        return ['server']
    if method in (
        'observe.health',
        'observe.afk_site',
        'observe.biome',
        'observe.container',
        'observe.containers',
        'observe.region',
        'observe.actors',
        'observe.actor_state',
        'observe.block_entity',
        'machine.control',
        'experiment.conditions',
        'experiment.restore',
    ) or method.startswith(('command.', 'clock.', 'counter.', 'fake_player.')):
        return ['server']
    if method.startswith('experiment.'):
        return ['local']
    return ['client']


# Discovery/knowledge service names retain their existing public membership.
READ_SERVICES = frozenset(
    (
        'environment.describe',
        'environment.document',
        'environment.status',
        'actions.list',
        'actions.get',
        'schemas.get',
        'tasks.list',
        'tasks.get',
        'feedback.get',
        'feedback.list',
        'registry.get',
    )
)
READ_ONLY_OPERATIONS = READ_SERVICES | {'evaluation.get', 'submission.validate'}
_READ_ONLY_ACTIONS = frozenset(
    (
        'experiment.wait',
        'command.describe',
        'clock.query',
        'counter.read',
        'fake_player.list',
        'fake_player.status',
        'fake_player.inventory',
        'session.describe',
    )
)


def action_effect(method):
    """Published primitive effect; unknown names are always conservative."""
    if method not in TOOLS:
        return 'mutation'
    return (
        'read_only'
        if (
            method in KNOWLEDGE_METHODS | LOCAL_DIAGNOSTICS
            or method.startswith('observe.')
            or method in _READ_ONLY_ACTIONS
        )
        else 'mutation'
    )


def request_effect(request):
    """Classify safety, never grant permission or validate action parameters.

    Disabled client actions and unknown/malformed names cannot acquire a read-only
    retry permission merely by looking like a published observation method.
    """
    if not isinstance(request, dict):
        return 'mutation'
    if request.get('op') != 'action.execute':
        return 'read_only' if request.get('op') in READ_ONLY_OPERATIONS else 'mutation'
    params = request.get('params', {})
    if not isinstance(params, dict):
        return 'mutation'
    action = params.get('action', {})
    if not isinstance(action, dict):
        return 'mutation'
    method = action.get('method')
    side = action.get('side')
    if not isinstance(method, str) or method not in TOOLS:
        return 'mutation'
    allowed = action_sides(method)
    if side == 'client' or allowed == ['client'] or side not in allowed:
        return 'mutation'
    return action_effect(method)
