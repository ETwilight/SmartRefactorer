"""Successive machine trials with one persistent run, inventory and physical machine."""

import copy
import hashlib
import json
import random
import time
from pathlib import Path

if __package__:
    from environments.techmc.contracts import VERSION
    from environments.techmc.contracts import validate
    from environments.techmc.runtime_support import ROOT
    from environments.techmc.runtime_support import atomic_json
    from environments.techmc.task_catalog import stable_hash
else:
    from contracts import VERSION, validate
    from runtime_support import ROOT, atomic_json
    from task_catalog import stable_hash


CONTROL_CHECKS = {'machine_modes', 'configurable_batches', 'brew_orders'}
STRUCTURAL_CHECKS = {'integrated_head_disc_machine', 'shared_tree_machine', 'shared_processing'}


def trials_for(task):
    if task.get('mode_trials'):
        return copy.deepcopy(task['mode_trials'])
    if task.get('brew_orders'):
        return [
            {
                'id': o['id'],
                'products': [o['item']],
                'kind': 'batch',
                'quantity': o['count'],
                'potion': o['potion'],
            }
            for o in task['brew_orders']
        ]
    if task.get('order_batch'):
        return [
            {
                'id': p,
                'products': [p],
                'kind': 'batch',
                'quantity': task['order_batch']['units'],
            }
            for p in task['primary_products']
        ]
    requirements = task.get('mode_requirements', {})
    if requirements.get('measurement_per_mode'):
        species = requirements.get('species')
        if species:
            return [
                {'id': s, 'products': ['minecraft:' + s + '_log'], 'kind': 'continuous'}
                for s in species
            ]
        return [
            {'id': p, 'products': [p], 'kind': 'continuous'} for p in task['primary_products']
        ]
    return []


def required_review(task):
    return list(
        dict.fromkeys(
            task.get('mandatory_review_checks', [])
            + [
                c
                for c in task.get('acceptance', {}).get('required_checks', [])
                if c in STRUCTURAL_CHECKS
            ]
        )
    )


def evaluation_mode(task, requested='auto'):
    if requested not in ('auto', 'deterministic', 'hybrid'):
        raise ValueError('Unknown evaluation mode')
    required = required_review(task)
    return {
        'requested': requested,
        'effective': (
            ('hybrid' if required else 'deterministic') if requested == 'auto' else requested
        ),
        'required_review_checks': required,
    }


def within(bounds, pos):
    return all(a <= v <= b for a, v, b in zip(bounds['min'], pos, bounds['max']))


def validate_review_volumes(task, submission, max_blocks=32768):
    volumes = submission.get('review_volumes', [])
    if not volumes:
        if required_review(task):
            raise ValueError(
                'Structural acceptance requires declared review_volumes containing the actual shared machinery'
            )
        return
    regions = [
        {'dimension': submission['dimension'], 'bounds': submission['bounds']}
    ] + submission.get('regions', [])
    total = 0
    for volume in volumes:
        bounds = volume['bounds']
        size = [b - a + 1 for a, b in zip(bounds['min'], bounds['max'])]
        if min(size) < 1:
            raise ValueError('Review volume bounds must be ordered')
        count = size[0] * size[1] * size[2]
        total += count
        if count > 4096:
            raise ValueError('A review volume may inspect at most 4096 blocks')
        if not any(
            r['dimension'] == volume['dimension']
            and within(r['bounds'], bounds['min'])
            and within(r['bounds'], bounds['max'])
            for r in regions
        ):
            raise ValueError('Review volumes must be contained in declared machine regions')
    if total > max_blocks:
        raise ValueError('Structural evidence exceeds the review block budget')


def validate_mode_submission(task, submission):
    regions = [
        {'dimension': submission['dimension'], 'bounds': submission['bounds']}
    ] + submission.get('regions', [])
    for control in submission.get('start_controls', []):
        dimension = control.get('dimension', submission['dimension'])
        if not any(
            r['dimension'] == dimension and within(r['bounds'], control['position'])
            for r in regions
        ):
            raise ValueError('Start controls must be inside declared machine regions')
        if control['action'] == 'set_lever' and type(control.get('powered')) is not bool:
            raise ValueError('Lever controls require a boolean powered state')
        if control['action'] == 'press_button' and 'powered' in control:
            raise ValueError('Button presses do not accept a powered override')
    trials = trials_for(task)
    if not trials:
        return
    declared = submission.get('machine_modes', {})
    if set(declared) != {t['id'] for t in trials}:
        raise ValueError(
            'Declare machine controls for every published mode or order, without extra modes'
        )
    regions = [
        {'dimension': submission['dimension'], 'bounds': submission['bounds']}
    ] + submission.get('regions', [])
    planting = set()
    for trial in trials:
        binding = declared[trial['id']]
        if not binding['controls']:
            raise ValueError('Each mode requires at least one existing machine control')
        if trial['kind'] == 'batch' and not any(
            c['action'] == 'press_button' for c in binding['controls']
        ):
            raise ValueError('Each batch order requires an existing order-trigger button')
        for control in binding['controls']:
            dimension = control.get('dimension', submission['dimension'])
            if not any(
                r['dimension'] == dimension and within(r['bounds'], control['position'])
                for r in regions
            ):
                raise ValueError('Machine controls must be inside declared machine regions')
            if control['action'] == 'set_lever' and type(control.get('powered')) is not bool:
                raise ValueError('Lever controls require a boolean powered state')
            if control['action'] == 'press_button' and 'powered' in control:
                raise ValueError('Button presses do not accept a powered override')
        operation = binding.get('operation', submission['operation'])
        if task.get('mode_requirements', {}).get('species'):
            if (
                operation['driver'] != 'use'
                or operation['tool'] != 'minecraft:' + trial['id'] + '_sapling'
                or operation.get('planting_footprint', 'single') != 'single'
            ):
                raise ValueError(
                    'Each tree mode must use its matching single-sapling planting operation'
                )
            planting.add((submission['dimension'], tuple(operation['target'])))
    limit = task.get('mode_requirements', {}).get('max_planting_positions')
    if limit is not None and len(planting) > limit:
        raise ValueError('Tree modes exceed the published maximum number of planting positions')


def apply_controls(call, submission, binding, phase):
    for control in binding['controls']:
        if (control['action'] == 'press_button') != (phase == 'trigger'):
            continue
        dimension = control.get('dimension', submission['dimension'])
        params = {
            **dict(zip(('x', 'y', 'z'), control['position'])),
            'dimension': dimension,
            'action': control['action'],
        }
        if 'powered' in control:
            params['powered'] = control['powered']
        result = call('machine.control', params)
        if result.get('source') != 'vanilla_machine_control':
            raise RuntimeError('Native machine-control telemetry unavailable')


def count_components(task, submission, values, order=None):
    """Return all delivered item/variant counts, deduplicating chest halves and boxes."""
    seen = set()
    counts = {}
    selected = 0
    refs = {stable_hash(ref) for out in submission['outputs'] for ref in out['containers']}
    for key in refs:
        snapshot = values[key]
        if snapshot.get('item_components_version') != 'item-components-v1':
            raise RuntimeError('Component-sensitive delivery telemetry unavailable')
        for slot in snapshot['slots'] + [
            c for s in snapshot['slots'] for c in s.get('shulker_slots', [])
        ]:
            if slot['slot_id'] in seen:
                continue
            seen.add(slot['slot_id'])
            item = slot['item']
            quantity = slot['count']
            if not quantity or item.endswith('shulker_box'):
                continue
            components = slot.get('item_components', {})
            identity = (
                item + '|' + json.dumps(components, sort_keys=True, separators=(',', ':'))
            )
            counts[identity] = counts.get(identity, 0) + quantity
            if order and item in order['products']:
                potion = components.get('minecraft:potion_contents', {})
                if not order.get('potion') or (
                    potion.get('potion') == order['potion']
                    and potion.get('custom_effect_count') == 0
                ):
                    selected += quantity
    return {'all': counts, 'selected': selected}


def exact_delivery(before, after, quantity):
    deltas = {
        key: after['all'].get(key, 0) - before['all'].get(key, 0)
        for key in set(before['all']) | set(after['all'])
    }
    selected = after['selected'] - before['selected']
    return {
        'requested': quantity,
        'selected_delta': selected,
        'delivered_delta': deltas,
        'passed': selected == quantity
        and sum(deltas.values()) == quantity
        and all(n >= 0 for n in deltas.values()),
    }


class ModeSession:
    """Children share the parent's native owner and heartbeat; only their own actors retire."""

    def __init__(self, base, index):
        self.base = base
        self.run_id = base.run_id + '-trial-' + str(index)
        self.server = base.server
        self.created = set()

    def invoke(self, step):
        result = self.base.invoke(step)
        if step['method'] == 'fake_player.spawn':
            self.created.add(step['params']['id'])
        return result

    @property
    def _legacy_evaluation_snapshot_fixture(self):
        return getattr(self.base, '_legacy_evaluation_snapshot_fixture', False)

    def cleanup(self):
        errors = []
        for name in self.created:
            for action in ('drop_inventory', 'stop', 'remove'):
                try:
                    self.base.invoke(
                        {
                            'method': 'fake_player.' + action,
                            'params': {'id': name},
                            'side': 'server',
                            'phase': 'measure',
                        }
                    )
                except Exception as exc:
                    errors.append(str(exc))
            deadline = time.monotonic() + 15
            while True:
                try:
                    actors = self.server.call('observe.actors')['result']
                except Exception as exc:
                    errors.append(str(exc))
                    break
                if not any(p['name'] == name for p in actors):
                    break
                if time.monotonic() > deadline:
                    errors.append('Mode actor did not disconnect after removal')
                    break
                time.sleep(0.05)
        return errors


def child_task(task, trial):
    value = copy.deepcopy(task)
    value.pop('mode_trials', None)
    value.pop('brew_orders', None)
    value.pop('order_batch', None)
    value.pop('mandatory_review_checks', None)
    if value.get('mode_requirements'):
        value['mode_requirements']['measurement_per_mode'] = False
    handled = CONTROL_CHECKS | STRUCTURAL_CHECKS
    value['acceptance']['required_checks'] = [
        c for c in value['acceptance']['required_checks'] if c not in handled
    ]
    value['primary_products'] = trial['products']
    value['measurement'].pop('aggregate_targets', None)
    value['measurement']['product_targets'] = {
        p: v
        for p, v in value['measurement'].get('product_targets', {}).items()
        if p in trial['products']
    }
    if trial['kind'] == 'batch':
        limit = task.get('order_window_ticks', task['measurement']['window_ticks'])
        value['measurement'].update(
            window_ticks=limit,
            min_net_items=trial['quantity'],
            min_per_game_hour=trial['quantity'] * 72000 / limit,
            segments=1,
        )
        value['measurement'].pop('product_targets', None)
        value['storage']['reserved_stack_slots_per_primary'] = min(
            value['storage']['reserved_stack_slots_per_primary'], trial['quantity']
        )
    if task.get('mode_requirements', {}).get('species'):
        value['replanting_items'] = ['minecraft:' + trial['id'] + '_sapling']
        value['_suite_balance_items'] = copy.deepcopy(task['replanting_items'])
    return value


def run_modes(evaluator, instance, submission, tick_mode):
    task = instance['task']
    session = evaluator.session
    directory = ROOT / 'runs' / session.run_id
    directory.mkdir(parents=True, exist_ok=True)
    report = {
        'protocol_version': VERSION,
        'evaluation_id': session.run_id,
        'task_instance_id': instance['id'],
        'task_id': task['id'],
        'task_hash': task['task_hash'],
        'submission_hash': stable_hash(submission),
        'verdict': 'inconclusive',
        'goal_passed': None,
        'check_pass_fraction': 0,
        'checks': [],
        'metrics': {},
        'ai_review': None,
        'evidence_path': str(directory),
        'cleanup_errors': [],
    }
    evidence = {'task': task, 'submission': submission, 'observations': []}

    def check(name, status, message):
        report['checks'].append(
            {
                'id': name,
                'status': status,
                'message': message,
                'evidence': ['evidence.json#/observations'],
            }
        )

    try:
        evaluator.validate(instance, submission)
        reviews = required_review(task)
        if reviews and evaluator.reviewer is None:
            check(
                'required_structural_review',
                'inconclusive',
                'This task requires hybrid evaluation with native review-volume evidence; deterministic counts cannot establish shared machinery',
            )
            return report
        trials = trials_for(task)
        random.Random(hashlib.sha256(session.run_id.encode()).digest()).shuffle(trials)
        if (
            task.get('mode_requirements', {}).get('repeat_previous_mode')
            or task.get('mode_requirements', {}).get('rebuild_between_modes') is False
        ):
            trials.append(copy.deepcopy(trials[0]))
        reports = []
        for index, trial in enumerate(trials):
            mode_task = child_task(task, trial)
            s = copy.deepcopy(submission)
            s.pop('machine_modes', None)
            binding = submission['machine_modes'][trial['id']]
            s['operation'] = copy.deepcopy(binding.get('operation', submission['operation']))
            # Preserve all destinations for unexpected delivery checks, while scoring only this mode's product.
            s['outputs'] = [o for o in s['outputs'] if o['product'] in trial['products']]
            if not s['outputs']:
                raise ValueError('Every mode product needs a declared output destination')
            child = type(evaluator)(
                ModeSession(session, index), evaluator.max_volume, None, evaluator.max_chunks
            )
            context = {
                'index': index,
                'trial': trial,
                'binding': binding,
                'original_task': task,
                'original_submission': submission,
                'skip_initial_checks': index > 0,
                'review_at_parent': True,
            }
            result = child._run_single(
                {**instance, 'task': mode_task}, s, tick_mode, _mode_context=context
            )
            reports.append({'mode': trial['id'], 'trial_index': index, 'result': result})
            if result.get('metrics', {}).get('infrastructure_failure'):
                report['metrics']['infrastructure_failure'] = copy.deepcopy(
                    result['metrics']['infrastructure_failure']
                )
            if result.get('infrastructure', {}).get('measurement_valid') is False:
                report['infrastructure'] = copy.deepcopy(result['infrastructure'])
            evidence_file = Path(result['evidence_path']) / 'evidence.json'
            if evidence_file.exists():
                try:
                    observed = json.loads(evidence_file.read_text(encoding='utf-8'))
                    for observation in observed['observations']:
                        evidence['observations'].append(
                            {
                                **observation,
                                'id': 'trial-' + str(index) + '-' + observation['id'],
                                'trial_id': trial['id'],
                            }
                        )
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise RuntimeError('Mode evidence unavailable: ' + str(exc)) from exc
            check(
                'trial_' + str(index),
                'pass' if result['verdict'] == 'pass' else result['verdict'],
                'Mode '
                + trial['id']
                + ' must satisfy its own production, operation and accounting requirements',
            )
            if result['verdict'] != 'pass':
                break
        report['metrics']['mode_trials'] = reports
        report['metrics']['production'] = {
            str(i): r['result']['metrics'].get('production', {}) for i, r in enumerate(reports)
        }
        complete = len(reports) == len(trials) and all(
            r['result']['verdict'] == 'pass' for r in reports
        )
        if complete and task.get('mode_requirements', {}).get('species'):
            before = reports[0]['result']['metrics'].get('suite_initial_stocks')
            after = reports[-1]['result']['metrics'].get('suite_final_stocks')
            items = task.get('replanting_items', [])
            if (
                not isinstance(before, dict)
                or not isinstance(after, dict)
                or set(before) != set(items)
                or set(after) != set(items)
            ):
                raise RuntimeError('Complete suite-wide sapling stock telemetry unavailable')
            required = {
                item: (
                    0
                    if item == 'minecraft:jungle_sapling'
                    else task.get('replanting_growth', {}).get('minimum_net_increase', 1)
                )
                for item in items
            }
            report['metrics']['suite_seed_balance'] = {
                'before': before,
                'after': after,
                'required_net_change': required,
            }
            seed_ok = all(after[item] - before[item] >= required[item] for item in items)
            check(
                'suite_seed_balance',
                'pass' if seed_ok else 'fail',
                'The full mode suite must preserve all six sapling stocks and grow non-jungle stocks, including operating-player inventory',
            )
        suite_status = (
            'pass'
            if complete
            else (
                'fail'
                if any(r['result']['verdict'] == 'fail' for r in reports)
                else 'inconclusive'
            )
        )
        for name in CONTROL_CHECKS.intersection(
            task.get('acceptance', {}).get('required_checks', [])
        ) | {'machine_modes'}:
            check(
                name,
                suite_status,
                'Every published mode and requested repeat must work through existing controls without restocking or rebuilding',
            )
        if task.get('brew_orders'):
            check(
                'item_components',
                suite_status,
                'Every requested potion identity, bottle form and three-bottle quantity is checked from native component telemetry',
            )
        if complete and evaluator.reviewer:
            try:
                review = evaluator.reviewer.review(task, submission, evidence, report)
            except Exception as exc:
                report['infrastructure'] = {
                    'status': 'review_unavailable',
                    'stage': 'review',
                    'recovered': False,
                    'fault': {'type': type(exc).__name__, 'message': str(exc)},
                }
                raise RuntimeError('Structural review unavailable: ' + str(exc)) from exc
            report['ai_review'] = review
            check(
                'ai_evidence_review',
                review['verdict'],
                'Mandatory shared-machine review uses the native geometry and successive trial evidence',
            )
            by_id = {c['id']: c for c in review.get('checks', [])}
            for name in reviews:
                item = by_id.get(name)
                check(
                    name,
                    item['verdict'] if item else 'inconclusive',
                    item['reason'] if item else 'Required structural review was omitted',
                )
    except (ValueError, AssertionError) as exc:
        check('acceptance_error', 'fail', str(exc))
    except Exception as exc:
        report['metrics']['infrastructure_failure'] = {
            'type': type(exc).__name__,
            'message': str(exc),
        }
        check('measurement_unavailable', 'inconclusive', str(exc))
    finally:
        try:
            report['cleanup_errors'] = session.cleanup()
        except Exception as exc:
            report['cleanup_errors'] = [str(exc)]
            report['metrics'].setdefault(
                'infrastructure_failure', {'type': type(exc).__name__, 'message': str(exc)}
            )
        checks = report['checks']
        report['check_pass_fraction'] = (
            sum(c['status'] == 'pass' for c in checks) / len(checks) if checks else 0
        )
        report['verdict'] = (
            'fail'
            if any(c['status'] == 'fail' for c in checks)
            else (
                'inconclusive'
                if report['cleanup_errors']
                or any(c['status'] == 'inconclusive' for c in checks)
                or 'production' not in report['metrics']
                else 'pass'
            )
        )
        if report['metrics'].get('infrastructure_failure'):
            report['verdict'] = 'inconclusive'
        report['goal_passed'] = (
            None if report['verdict'] == 'inconclusive' else report['verdict'] == 'pass'
        )
        if __package__:
            from environments.techmc.failure_contract import report as canonical_report
        if __package__:
            from environments.techmc.failure_contract import write_measurement
        else:
            from failure_contract import report as canonical_report, write_measurement
        normalized = canonical_report(report)
        report.clear()
        report.update(normalized)
        evidence['result'] = report
        write_measurement(directory, evidence, report)
    return report
