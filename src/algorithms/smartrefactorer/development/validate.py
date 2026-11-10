"""Validate：对候选的语法、接口、行为保持与结构目标逐项生成检查证据。

每项证据绑定 candidate_hash。可执行检查给出 PASS 或 FAIL，缺少执行依据
的检查保留 PENDING；这些记录决定下一步执行还是进入 Reflect。
"""

import copy
import json
from ..library import digest
from jsonschema import Draft202012Validator, SchemaError
from ..execution import python_backend
from ..execution.legacy_step import parse, step
from .check_contracts import diagnostics as check_diagnostics, run_python_check


def validate(controller):
    """Check the candidate version and leave missing executable evidence PENDING."""
    state = controller.state
    checks = []
    # 整个验证过程共用一个候选哈希，发布时据此关联检查与实际代码。
    ch = digest(state.candidate)

    def add(name, status, observed):
        checks.append(
            {'id': name, 'candidate_hash': ch, 'status': status, 'observed': observed}
        )

    try:
        exports = state.proposal.get('library_exports')
        if state.context_profile == 'affected-analysis-and-observation-v11' and exports is None:
            raise ValueError(
                'Proposal must explicitly choose library_exports; [] keeps task-only code unpublished'
            )
        if exports is not None and (
            not isinstance(exports, list)
            or any((not isinstance(n, str) or n not in state.candidate for n in exports))
        ):
            raise ValueError('Invalid library export selection')
        for n, c in state.candidate.items():
            (python_backend.parse if c.get('language') == python_backend.PROFILE else parse)(
                c['source']
            )
            Draft202012Validator.check_schema(c.get('interface', {}).get('args_schema', {}))
            if not set(c['dependencies']) <= set(state.candidate):
                raise ValueError('Unresolved dependency ' + n)
        entry = (
            state.proposal['task_entry']['component_id']
            if state.proposal.get('task_entry')
            else None
        )
        if state.refactor_opportunity is None and entry not in state.candidate:
            raise ValueError('Missing execution entry')
        add('syntax-and-bindings', 'PASS', True)
    except (ValueError, KeyError, SyntaxError, SchemaError) as exc:
        add('syntax-and-bindings', 'FAIL', str(exc))
    fixtures = state.proposal.get('preservation_checks', [])
    preservation_covered = set()
    # 行为保持检查聚焦发生源码或接口变化的已提交组件。
    changed_old = set()
    for name, old in controller.library.manifest['components'].items():
        current = state.candidate.get(name, {})
        changed = any(
            (
                current.get(k) != old.get(k)
                for k in ('hash', 'interface', 'contract', 'dependencies')
            )
        )
        if changed:
            changed_old.add(name)
    for i, f in enumerate(fixtures):
        try:
            malformed = check_diagnostics(
                {'preservation_checks': [f]}, controller.library.manifest['components']
            )
            if malformed:
                add(
                    'fixture-' + str(i),
                    'PENDING',
                    {
                        'reason': 'Check specification is not executable',
                        'diagnostics': malformed,
                    },
                )
                continue
            if f.get('kind') == 'python_check':
                invocation, fixture_hash = run_python_check(controller, f)
                add(
                    'fixture-' + str(i),
                    'PASS' if invocation['result'] is True else 'FAIL',
                    {
                        'fixture_hash': fixture_hash,
                        'actual': invocation['result'],
                        'expected': True,
                        'requests': invocation['requests'],
                    },
                )
                continue
            n = f['component_id']
            values = [f.get('state', {}), f.get('feedback'), f.get('args', {})]
            normal = state.candidate[n].get('language') == python_backend.PROFILE
            invocation = (
                controller.run_fixture(state.candidate, n, values[2], f.get('requests', []))
                if normal
                else None
            )
            actual = (
                invocation['result'] if normal else step(state.candidate[n]['source'], *values)
            )
            old = controller.library.manifest['components'].get(n)
            if old:
                old_schema = old.get('interface', {}).get('args_schema', {})
                new_schema = state.candidate[n].get('interface', {}).get('args_schema', {})
                Draft202012Validator.check_schema(old_schema)
                Draft202012Validator.check_schema(new_schema)
                # 旧接口接受的 fixture 输入同时用于新接口，比较覆盖原有调用域。
                old_valid = Draft202012Validator(old_schema).is_valid(values[2])
                if old_valid and (not Draft202012Validator(new_schema).is_valid(values[2])):
                    add(
                        'fixture-' + str(i),
                        'FAIL',
                        {
                            'reason': 'An input accepted by the old interface is rejected by the new interface',
                            'args': values[2],
                        },
                    )
                    continue
                if normal and old.get('language') == python_backend.PROFILE:
                    old_invocation = controller.run_fixture(
                        controller.library.manifest['components'],
                        n,
                        values[2],
                        f.get('requests', []),
                    )
                    expected = old_invocation['result']
                    # 整次调用比较包含返回值和请求序列，两者共同表达可观测行为。
                    same = invocation == old_invocation
                elif normal:
                    add(
                        'fixture-' + str(i),
                        'PENDING',
                        'Cross-profile preservation requires explicit whole-invocation evidence',
                    )
                    continue
                else:
                    expected = step(old['source'], *values)
                    same = actual == expected
                add(
                    'fixture-' + str(i),
                    'PASS' if same else 'FAIL',
                    {'actual': actual, 'expected': expected},
                )
                if old_valid and same:
                    preservation_covered.add(n)
            elif 'assertions' in f:
                assertions = f['assertions']
                if not assertions or any(
                    (
                        not isinstance(a, dict)
                        or set(a) != {'path', 'equals'}
                        or (not isinstance(a['path'], str))
                        for a in assertions
                    )
                ):
                    add(
                        'fixture-' + str(i),
                        'PENDING',
                        'Assertions must be executable {path,equals} records',
                    )
                    continue
                observed = []
                for assertion in assertions:
                    value = actual
                    pointer = assertion['path']
                    if pointer and (not pointer.startswith('/')):
                        raise ValueError('Assertion path must be JSON Pointer')
                    for token in pointer.split('/')[1:] if pointer else []:
                        token = token.replace('~1', '/').replace('~0', '~')
                        value = value[int(token)] if isinstance(value, list) else value[token]
                    observed.append(
                        {'path': pointer, 'actual': value, 'expected': assertion['equals']}
                    )
                add(
                    'fixture-' + str(i),
                    'PASS' if all((a['actual'] == a['expected'] for a in observed)) else 'FAIL',
                    observed,
                )
            else:
                expected = f['expected']
                if (
                    not normal
                    and isinstance(expected, dict)
                    and ('assertions' in expected or 'state_assertions' in expected)
                ):
                    add(
                        'fixture-' + str(i),
                        'PENDING',
                        'Narrative check fields are not executable; specify exact expected or typed path assertions',
                    )
                else:
                    add(
                        'fixture-' + str(i),
                        'PASS' if actual == expected else 'FAIL',
                        {'actual': actual, 'expected': expected},
                    )
        except python_backend.FixtureEvidenceMissing as exc:
            add('fixture-' + str(i), 'PENDING', str(exc))
        except python_backend.FixtureExecutionError as exc:
            add('fixture-' + str(i), 'FAIL', exc.details)
        except (ValueError, KeyError, TypeError, IndexError, SchemaError) as exc:
            add('fixture-' + str(i), 'FAIL', str(exc))
    for name in sorted(changed_old - preservation_covered):
        add(
            'preserve-' + name,
            'PENDING',
            'No passing preservation fixture on a valid old-interface input',
        )
    for i, check in enumerate(state.proposal.get('structural_checks', [])):
        if (
            not isinstance(check, dict)
            or check.get('kind') not in ('calls', 'absent_call')
            or (not isinstance(check.get('caller'), str))
            or (not isinstance(check.get('callee'), str))
        ):
            add(
                'structure-' + str(i),
                'PENDING',
                {
                    'reason': 'No implemented checker for this specification',
                    'specification': check,
                },
            )
            continue
        if check['caller'] not in state.candidate:
            add(
                'structure-' + str(i),
                'PENDING',
                {'reason': 'Structural checker caller is unavailable', 'specification': check},
            )
            continue
        caller = state.candidate[check['caller']]
        if caller.get('language') == python_backend.PROFILE:
            references_found = python_backend.references(caller['source'], check['caller'])
            exists = check['callee'] in references_found['call_sites']
            if not exists and references_found['unresolved_dynamic_calls']:
                add(
                    'structure-' + str(i),
                    'PENDING',
                    {
                        'reason': 'Dynamic dependencies need invocation evidence',
                        'specification': check,
                    },
                )
                continue
        else:
            exists = check['callee'] in caller['dependencies']
        passed = exists if check['kind'] == 'calls' else not exists
        add('structure-' + str(i), 'PASS' if passed else 'FAIL', check)
    for i, blocker in enumerate(state.proposal.get('blockers', [])):
        add('proposal-blocker-' + str(i), 'PENDING', blocker)
    return checks
