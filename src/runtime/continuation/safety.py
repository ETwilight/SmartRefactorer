"""Recognize failed continuation bootstraps whose completed environment work was
discovery-only.
"""

import json
from pathlib import Path


def clean_discovery_only_failure(source, report):
    """Check the event ledger and cleanup record for a discovery-only failed segment.

    Require one continuation anchor, paired completed discovery requests, and
    unchanged carried model usage. This classification lets the caller proceed
    to the separate world-ownership and continuation-accounting checks.
    """

    cleanup = report.get('cleanup') or {}
    if (
        report.get('status') != 'failed'
        or not cleanup.get('closed')
        or cleanup.get('cleanup_errors')
        or report.get('finalization_errors')
        or report.get('method_summary_error')
    ):
        return False
    anchor = None
    # pending: dict[str, dict], mapping outstanding environment call IDs to their discovery
    # requests.
    pending = {}
    completed = 0
    try:
        with (Path(source) / 'log/events.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                event = json.loads(line)
                kind = event.get('kind')
                if kind == 'continuation.start':
                    if anchor is not None:
                        return False
                    anchor = event
                elif kind == 'env.start':
                    call = event['call']
                    request = call['request']
                    if (
                        call.get('stage') != 'discovery'
                        or request.get('op')
                        not in ('environment.describe', 'actions.list', 'tasks.list')
                        or call['call_id'] in pending
                    ):
                        return False
                    pending[call['call_id']] = request
                elif kind == 'env.finish':
                    call = event['call']
                    if (
                        pending.pop(call['call_id'], None) != call['request']
                        or call.get('feedback', {}).get('status') != 'completed'
                    ):
                        return False
                    completed += 1
                elif kind not in ('round.finish', 'audit.finish', 'run.finish'):
                    # Reject model, program, and other events outside the discovery bootstrap
                    # sequence.
                    return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    usage = report.get('token_usage') or {}
    return bool(
        anchor
        and completed
        and not pending
        and usage.get('budget_bounded_complete') is True
        and usage.get('charged_tokens') == anchor.get('carried_tokens')
        and report.get('model_calls') == anchor.get('carried_model_calls')
    )
