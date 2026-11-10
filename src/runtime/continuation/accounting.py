"""续跑的分段用量汇总：读取原始模型记录，核对累计费用与原有上限。

同一 call_id 的重复存档合并为一次调用，冲突记录交给调用方处理。
"""

import hashlib
import json
from pathlib import Path

from audit.recorder import budget_bounded


def _integer(value):
    return type(value) is int and value >= 0


def _same(a, b):
    return json.dumps(a, sort_keys=True, separators=(',', ':'), allow_nan=False) == json.dumps(
        b, sort_keys=True, separators=(',', ':'), allow_nan=False
    )


def _equivalent_limits(left, right):
    # These optional fields have an existing missing == None meaning. Non-None
    # values and all other keys remain exact; never silently loosen a cap.
    def normalized(value):
        return {
            k: v
            for k, v in value.items()
            if not (k in ('round_tokens', 'max_task_attempts') and v is None)
        }

    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and normalized(left) == normalized(right)
    )


def _segments(source, report):
    original_limits = report.get('limits')
    seen = set()
    while True:
        if source in seen:
            raise ValueError('Continuation ancestry cycle')
        seen.add(source)
        yield source, report
        continuation = report.get('continuation') or {}
        if not continuation.get('source_report_sha256'):
            return
        launch = json.loads((source / 'launch.json').read_text(encoding='utf-8'))
        method = {'agent_baseline': 'baseline', 'psn_compare': 'psn'}.get(
            report.get('workflow')
        )
        predecessor = (launch.get('options') or {}).get(str(method) + '_resume_from')
        if not predecessor:
            raise ValueError('Cannot locate original carried call evidence')
        predecessor = Path(predecessor).resolve()
        raw = (predecessor / 'report.json').read_bytes()
        previous = json.loads(raw)
        if (
            hashlib.sha256(raw).hexdigest() != continuation['source_report_sha256']
            or previous.get('run_id') != continuation.get('source_run_id')
            or previous.get('workflow') != report.get('workflow')
            or not _equivalent_limits(previous.get('limits'), original_limits)
        ):
            raise ValueError(
                'Continuation ancestry identity, accounting hash or original caps differ'
            )
        source, report = predecessor, previous


def _events(source):
    with (source / 'log/events.jsonl').open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                raise ValueError('Incomplete stopped event ledger')
            yield json.loads(line)


def collect_resume_model_calls(source, report):
    """Include canonical setup/maintenance calls outside any round, in real order."""
    models = {}
    groups = [record.get('model_calls', []) for record in report.get('rounds', [])]
    groups.extend(
        report[key] for key in ('models', 'model_calls') if isinstance(report.get(key), list)
    )
    for group in groups:
        for call in group:
            cid = call.get('call_id')
            if not isinstance(cid, str) or not cid:
                raise ValueError('Missing model-call identity')
            if cid in models and not _same(models[cid], call):
                raise ValueError('Conflicting carried model-call evidence')
            models[cid] = call
    ordered = []
    ordered_ids = set()
    for segment, _ in reversed(list(_segments(Path(source).resolve(), report))):
        for event in _events(segment):
            if event.get('kind') not in ('model.finish', 'model.not_sent'):
                continue
            call = event.get('call') or {}
            cid = call.get('call_id')
            if not isinstance(cid, str) or not cid:
                raise ValueError('Missing terminal model-call identity')
            if cid not in models:
                if not {'request', 'usage', 'charged_tokens', 'usage_status'} <= set(call):
                    raise ValueError(
                        'Setup/maintenance call lacks complete canonical accounting'
                    )
                models[cid] = call
            if cid not in ordered_ids:
                ordered.append(models[cid])
                ordered_ids.add(cid)
    ordered.extend(call for cid, call in models.items() if cid not in ordered_ids)
    return ordered


def validate_resume_accounting(source, report=None):
    """核对续跑模型记录、分段汇总与原运行限制，返回可继承的累计用量。"""
    source = Path(source).resolve()
    report = (
        json.loads((source / 'report.json').read_text(encoding='utf-8'))
        if report is None
        else report
    )
    models = {call['call_id']: call for call in collect_resume_model_calls(source, report)}
    charged = known = unknown = 0
    unknown_ids = set()
    for cid, call in models.items():
        if '_started' in call or not _integer(call.get('charged_tokens')):
            raise ValueError('Incomplete model-call accounting')
        status = call.get('usage_status')
        usage = call.get('usage')
        fee = call['charged_tokens']
        if status in ('reported', 'not_sent'):
            if (
                not isinstance(usage, dict)
                or not all(
                    _integer(usage.get(k))
                    for k in ('input_tokens', 'output_tokens', 'total_tokens')
                )
                or usage['total_tokens'] != usage['input_tokens'] + usage['output_tokens']
                or fee != usage['total_tokens']
            ):
                raise ValueError('Reported model usage does not reconcile')
            if status == 'not_sent' and fee != 0:
                raise ValueError('Unsent model call has a nonzero charge')
            known += fee
        elif status == 'unknown_reserved_upper_bound':
            if usage is not None or not budget_bounded(call):
                raise ValueError('Unknown paid usage has no verified finite bound')
            unknown += fee
            unknown_ids.add(cid)
        else:
            raise ValueError('In-flight or unclassified model usage')
        if not budget_bounded(call):
            raise ValueError('Model accounting is blocked or unbounded')
        charged += fee
    summary = report.get('token_usage', {})
    if (
        not _integer(summary.get('charged_tokens'))
        or summary['charged_tokens'] != charged
        or not _integer(summary.get('total_tokens'))
        or summary['total_tokens'] != known
        or summary.get('complete') is not (not unknown_ids)
        or summary.get('unknown_usage_upper_bound_tokens', 0) != unknown
    ):
        raise ValueError(
            'Continuation accounting summary does not reconcile with original calls'
        )
    if 'budget_bounded_complete' in summary and summary['budget_bounded_complete'] is not True:
        raise ValueError('Continuation summary reports unbounded accounting')
    limits = report.get('limits', {})
    if not _integer(limits.get('total_tokens')) or charged > limits['total_tokens']:
        raise ValueError('Carried charge exceeds original run token allocation')
    round_cap = limits.get('round_tokens')
    if round_cap is not None:
        if not _integer(round_cap):
            raise ValueError('Invalid original round token allocation')
        for record in report.get('rounds', []):
            if (
                sum(call.get('charged_tokens', 0) for call in record.get('model_calls', []))
                > round_cap
            ):
                raise ValueError('Carried round charge exceeds original allocation')
    canonical_unknown = set()
    canonical_models = set()
    round_models = {
        record.get('round'): {call.get('call_id') for call in record.get('model_calls', [])}
        for record in report.get('rounds', [])
    }
    for current, current_report in _segments(source, report):
        starts = set()
        finishes = set()
        env_starts = set()
        env_finishes = set()
        proofs = {}
        with (current / 'log/events.jsonl').open('rb') as stream:
            for line in stream:
                if not line.endswith(b'\n'):
                    raise ValueError('Incomplete stopped event ledger')
                event = json.loads(line)
                kind = event.get('kind')
                item = event.get('call') or {}
                cid = item.get('call_id')
                if kind == 'model.start':
                    if not isinstance(cid, str) or cid in starts:
                        raise ValueError('Duplicate or missing model-start identity')
                    starts.add(cid)
                elif kind in ('model.generation_bound', 'model.embedding_bound'):
                    proof_id = event.get('call_id')
                    if proof_id not in starts or proof_id in finishes:
                        raise ValueError('Model bound was not journaled before completion')
                    proofs[proof_id] = event.get('generation_bound') or event.get(
                        'embedding_bound'
                    )
                elif kind in ('model.finish', 'model.not_sent'):
                    if cid not in starts or cid in finishes:
                        raise ValueError('Orphan or duplicate model finish')
                    finishes.add(cid)
                    if cid not in models:
                        raise ValueError('Paid call omitted from carried report')
                    if event.get('round') is not None and cid not in round_models.get(
                        event['round'], set()
                    ):
                        raise ValueError(
                            'Round-attributed model call is missing from its original round'
                        )
                    canonical_models.add(cid)
                    if 'usage_status' in item and not _same(item, models[cid]):
                        raise ValueError(
                            'Carried model call differs from original canonical finish'
                        )
                    if cid in unknown_ids:
                        if not _same(item, models[cid]):
                            raise ValueError(
                                'Unknown paid call differs from original canonical finish'
                            )
                        proof = item.get('generation_bound') or item.get('embedding_bound')
                        if not proof or not _same(proofs.get(cid), proof):
                            raise ValueError(
                                'Unknown paid call lacks original bound attestation'
                            )
                        canonical_unknown.add(cid)
                elif kind == 'env.start':
                    env_starts.add(cid)
                elif kind == 'env.finish':
                    env_finishes.add(cid)
        if starts != finishes or env_starts != env_finishes:
            raise ValueError('In-flight request blocks continuation')
    if canonical_unknown != unknown_ids:
        raise ValueError('Unknown paid usage lacks canonical completed evidence')
    if canonical_models != set(models):
        raise ValueError('Carried model calls lack canonical completion identities')
    return {
        'charged_tokens': charged,
        'known_tokens': known,
        'unknown_usage': bool(unknown_ids),
        'unknown_usage_upper_bound_tokens': unknown,
        'budget_bounded_complete': True,
    }
