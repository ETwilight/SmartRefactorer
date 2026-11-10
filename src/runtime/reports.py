"""Common report rendering; no dependency on a method implementation."""

import json
from html import escape
from pathlib import Path
import uuid


class _HtmlSink:
    def __init__(self, stream):
        self.stream = stream

    def __iadd__(self, text):
        self.stream.write(text)
        return self


def write_html(folder, report):
    """Stream derived HTML; never hold a second complete run ledger in RAM."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / ('report.html.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        _write_html(folder, report, stream)
    temporary.replace(folder / 'report.html')


def _write_html(folder, report, stream):
    rows = []
    for r in report['rounds']:
        outcomes = '; '.join(
            str(o.get('task_id') or o.get('task_instance_id') or 'unknown') + ': ' + o['status']
            for o in r['task_outcomes']
        )
        wall_prefix = '≥ ' if r.get('wall_seconds_semantics') == 'lower_bound' else ''
        rows.append(
            f'<tr><td><a href="rounds/{r["round"]:04d}.json">{r["round"]}</a></td>'
            f'<td>{wall_prefix}{r["wall_seconds"]:.3f}</td><td>{r["env_wall_seconds"]:.3f}</td>'
            f'<td>{r["token_usage"]["total_tokens"]}<br>input {r["token_usage"]["input_tokens"]}, output {r["token_usage"]["output_tokens"]}'
            f'<br>cached {r["token_usage"]["cached_input_tokens"]}, writes {r["token_usage"].get("cache_write_tokens","unrecorded")}, reasoning {r["token_usage"]["reasoning_tokens"]}'
            f'<br>charged {r["token_usage"]["charged_tokens"]}, complete {r["token_usage"]["complete"]}</td><td>{escape(outcomes)}</td>'
            f'<td><a href="{escape(r["audit"].get("file","audit/"))}">projection</a></td></tr>'
        )
    page = _HtmlSink(stream)
    page += '<!doctype html><meta charset="utf-8"><title>Workflow run report</title>'
    page += '<style>body{font:16px system-ui;margin:32px;max-width:1400px}table{border-collapse:collapse}td,th{padding:10px;border:1px solid #bbb}pre{white-space:pre-wrap}details{margin:12px 0}</style>'
    page += f'<h1>Workflow run {escape(report["run_id"])}</h1><p>Status: {escape(report["status"])}</p>'
    if report.get('experiment_kind'):
        page += f'<p><strong>{escape(report["experiment_kind"])}</strong></p>'
    if report.get('recovery_notice'):
        page += (
            '<p><strong>'
            + escape(report['recovery_notice'])
            + ' Timing after the last persisted event is unknown.</strong></p>'
        )
    if report.get('post_run_actor_recovery'):
        page += '<p><strong>Machine acceptance only: post-run verification found the player dead. The baseline made zero respawn calls. The operator invoked respawn after this run; that recovery is excluded from workflow results.</strong></p>'
    page += f'<p>Total wall: {report["wall_seconds"]:.3f}s; audit: {report["audit_wall_seconds"]:.3f}s; tokens: {report["token_usage"]["total_tokens"]}; accounting complete: {report["token_usage"]["complete"]}</p>'
    if 'distinct_tasks_passed' in report:
        page += f'<p>Passed distinct tasks: {report["distinct_tasks_passed"]}; known task/profile pairs: {report["distinct_profiles_passed"]}; successful instances: {report["task_successes"]}. Profile accounting complete: {report["profile_results_complete"]}.</p>'
    if report['token_usage'].get('cost_estimate'):
        cost = report['token_usage']['cost_estimate']
        page += f'<p>Public-rate estimate USD: {escape(cost["minimum_usd"])} to {escape(str(cost["maximum_usd"]))}; excludes account-specific adjustments. Cache write/read counts are disjoint subsets of input tokens.</p>'
    page += '<p>Round wall time excludes audit; total run wall time includes audit and cleanup. Environment time includes any nested executor/reviewer model time. Unknown outcomes are not failures.</p>'
    page += (
        '<table><tr><th>Round</th><th>Wall seconds</th><th>Environment seconds</th><th>Tokens</th><th>Task outcomes</th><th>Audit</th></tr>'
        + ''.join(rows)
        + '</table>'
    )
    for r in report['rounds']:
        page += f'<h2>Round {r["round"]}</h2>'
        manifest = folder / r['audit'].get('file', 'audit/absent.json')
        if manifest.is_file():
            audit = json.loads(manifest.read_text(encoding='utf-8'))
            structures = audit.get('structure_repair', {}).get(
                'server_schematics', audit.get('schematics', [])
            )
            for structure in structures:
                if structure.get(
                    'source'
                ) == 'recorded_server_block_snapshot' and structure.get('artifact'):
                    target = (
                        (manifest.parent / structure['artifact']).relative_to(folder).as_posix()
                    )
                    page += f'<p><a href="{escape(target)}">Server structure: {escape(structure["artifact"])}</a> — {structure["non_air_blocks"]} non-air blocks, validated against recorded evidence.</p>'
        for call in r['model_calls']:
            page += (
                f'<details><summary>Model: {escape(call["scope"])} — {call["wall_seconds"]:.3f}s</summary><pre>'
                + escape(json.dumps(call, ensure_ascii=False, indent=2))
                + '</pre></details>'
            )
        for call in r['env_calls']:
            page += (
                f'<details><summary>{escape(str(call["request"].get("op","invalid")))} — {call["wall_seconds"]:.3f}s</summary><pre>'
                + escape(
                    json.dumps(
                        {'request': call['request'], 'feedback': call['feedback']},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                + '</pre></details>'
            )


def finalize_report(folder, report, *, render=None):
    """Persist authoritative JSON first; optional projections cannot erase it."""
    from audit.recorder import save_json
    from .layout import RunLayout

    folder = Path(folder)
    save_json(folder / 'report.json', report)
    errors = []
    try:
        (render or write_html)(folder, report)
    except Exception as exc:
        errors.append({'stage': 'report.html', 'type': type(exc).__name__, 'message': str(exc)})
    try:
        RunLayout(folder).finish(report)
    except Exception as exc:
        errors.append(
            {'stage': 'layout.projections', 'type': type(exc).__name__, 'message': str(exc)}
        )
    if errors:
        report.setdefault('projection_errors', []).extend(errors)
        save_json(folder / 'report.json', report)
        try:
            save_json(folder / 'reports/report.json', report)
        except OSError:
            pass
    return report
