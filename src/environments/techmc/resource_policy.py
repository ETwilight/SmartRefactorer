"""Agent-facing server resource facts and recovery guidance, not agent budgets."""

if __package__:
    from .server_recovery import binding
else:
    from server_recovery import binding


def describe(environment):
    try:
        state = binding(environment)
        metadata_status = 'managed_configuration' if state else 'unmanaged_or_unavailable'
    except Exception as exc:
        state = {}
        metadata_status = 'unavailable:' + type(exc).__name__
    heap = state.get('heap_mb')
    processors = state.get('processors')
    heap = heap if type(heap) is int and heap > 0 else None
    processors = processors if type(processors) is int and processors > 0 else None
    return {
        'scope': 'Minecraft server resources; research-agent processes and model budgets are owned by their experiment hosts',
        'metadata_status': metadata_status,
        'configured_limits': {
            'jvm_heap_max_bytes': heap * 1024 * 1024 if heap is not None else None,
            'jvm_active_processor_count': processors,
            'process_rss_limit_bytes': None,
            'host_memory_limit_bytes': None,
            'agent_process_memory_limit_bytes': None,
            'agent_context_token_limit': None,
        },
        'limit_semantics': 'Null means unknown or not supplied. JVM heap excludes native memory; ActiveProcessorCount is not CPU isolation or a CPU quota.',
        'evaluation_limits': {
            'max_volume_blocks': environment.max_evaluation_volume,
            'max_loaded_chunks': environment.max_evaluation_chunks,
            'scope': 'Declared acceptance regions and evaluator loading, not total world size or all loaded chunks',
        },
        'health_query': {
            'op': 'action.execute',
            'params': {'action': {'method': 'observe.health', 'side': 'server', 'params': {}}},
        },
        'agent_instructions': [
            'Respect the reported limits. Diagnose game load with observe.performance (MSPT and wall TPS) and observe.health (server memory/chunks). For your runner memory use local experiment.resources only when needed; these are distinct from model context or remote model memory.',
            'Use bounded reads and feedback summary/reference views. Recall needed fields with feedback.get using the supplied reference instead of repeatedly loading the full policy or evidence.',
            'Omit evaluation.run mode or use auto: the environment selects required structural review. Review needs host model credentials and budget; unavailable review is inconclusive, never a pass.',
            'The experiment host owns server allocation, heartbeat and shutdown. On busy, ownership mismatch or owner-lease expiry, report the blocker to the host; do not change ports or tamper with locks, credentials or server processes.',
            'A timeout is not proof of a crash. Follow execution.retry and infrastructure feedback; preserve the original request ID and payload for uncertain operations. Never blindly replay or replace a mutation. Inconclusive acceptance is unknown, even after successful recovery.',
            'Use server.recover only for the assigned experiment, outside acceptance, when recovery is indicated. If recovery fails, report it to the host. Reset only with explicit host authorization; after recovery inspect actual state and rerun interrupted acceptance, and after a reset start new task instances.',
        ],
    }
