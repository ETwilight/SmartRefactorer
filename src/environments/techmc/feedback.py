"""Lossless game-result projection: remove duplicate internal RPC transport fields."""


def public_result(value):
    if isinstance(value, list):
        return [public_result(item) for item in value]
    if not isinstance(value, dict):
        return value
    is_rpc = (
        value.get('source') in ('server', 'client')
        and 'run_id' in value
        and 'request_id' in value
        and 'method' in value
        and value.get('status') in ('ok', 'error', 'pending_or_unknown')
    )
    return {
        key: public_result(item)
        for key, item in value.items()
        if not (is_rpc and key in ('fingerprint', 'key'))
    }
