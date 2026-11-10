"""SmartRefactorer 算法主流程：Plan → Analyze → Propose → 实现 → Validate。

执行反馈进入 Reflect，再按诊断范围回到计划、方案或组件 Patch。任务验收后
发布选定组件，Detect 从发布差异与结构证据中寻找后续维护机会。
"""

import copy
from .library import digest
from .state import AlgorithmState, CheckpointView, Phase, StageResult
from . import retrieval
from .development import plan, analyze, propose, codegen, rewrite, validate
from .development.check_contracts import diagnostics as check_contract_diagnostics
from .repair import reflect, patch
from .operators import operator as invoke_operator, ROLE_GUIDES, LANGUAGE
from .execution import python_backend


class Controller:
    """组织任务开发、执行反馈、维修、发布与库维护。

    state 保存算法状态；ask、embed 与 save 由 adapter 注入。各阶段返回的
    结果在本控制器中汇合为下一次阶段转换。
    """

    def __init__(
        self,
        library,
        ask,
        embed,
        state=None,
        top_k=6,
        run_fixture=None,
        save=None,
        archive_accepted_state=None,
    ):
        self.library = library
        self.ask = ask
        self.embed = embed
        self.top_k = top_k
        self.run_fixture = run_fixture or python_backend.fixture
        self._save_callback = save
        self.archive_accepted_state = archive_accepted_state
        self.s = state or {
            'phase': 'idle',
            'parent': digest(library.manifest),
            'candidate': copy.deepcopy(library.manifest['components']),
            'p': None,
            'A': {},
            'P': None,
            'E': [],
            'r': None,
            'o': None,
            'history': [],
            'frames': [],
            'pending': None,
        }

    @property
    def s(self):
        return self._checkpoint

    @s.setter
    def s(self, document):
        self.state = AlgorithmState()
        self._checkpoint = CheckpointView(self.state, document)

    def save(self):
        if self._save_callback is not None:
            self._save_callback()

    def transition(self, phase):
        """Centralize phase changes; external receipts do not create new stages."""
        self.state.phase = Phase(phase)

    def operator(self, role, context):
        return invoke_operator(self, role, context)

    def retrieve(self, query):
        return retrieval.retrieve(self, query)

    def catalog(self):
        return {
            name: {
                key: component.get(key)
                for key in ('id', 'hash', 'interface', 'contract', 'dependencies')
            }
            for name, component in self.state.candidate.items()
        }

    def validate_plan(self, value):
        return plan.validate_plan(self, value)

    def plan(self, public):
        plan.plan(self, public)
        self.transition(Phase.ANALYZE)

    def analysis_context(self, component_id):
        return analyze.analysis_context(self, component_id)

    def affected_components(self):
        return analyze.affected_components(self)

    def _mentions(self, value, names):
        return analyze._mentions(self, value, names)

    def apply(self, delivery, component_id, expected_hash):
        codegen.apply_component(self, delivery, component_id, expected_hash)

    def validate(self):
        return validate.validate(self)

    def advance(self, public):
        """沿当前 phase 推进开发，返回阶段结果与候选可执行状态。

        复用计划决定 Analyze 范围，Propose 声明协调编辑，编辑游标连接 CodeGen/Rewrite
        与 Validate。检查结果再引出执行或 Reflect。
        """
        state = self.state
        # Plan 重入会重新选择复用关系；已有计划则从保存的阶段继续。
        if not state.reuse_plan or state.phase == Phase.PLAN:
            self.plan(public)
        if state.phase in (Phase.ANALYZE, Phase.PROPOSE):
            # 方案修订后刷新受影响的已有版本，分析缓存键决定哪些结果仍可复用。
            analyze.refresh_analyses(self, existing_only=state.phase == Phase.PROPOSE)
            self.transition(Phase.PROPOSE)
        if state.phase == Phase.PROPOSE:
            state.proposal = propose.propose(self, public)
            # Propose 的阻塞项先进入诊断，后续编辑以可落实的协调方案为起点。
            if state.proposal.get('blockers') and not state.proposal.get('edits'):
                state.validation_evidence = [
                    {
                        'id': 'proposal-blocker-' + str(index),
                        'candidate_hash': digest(state.candidate),
                        'status': 'PENDING',
                        'observed': blocker,
                    }
                    for index, blocker in enumerate(state.proposal['blockers'])
                ]
                self.transition(Phase.REPAIR)
                return StageResult(state.phase)
            state.edit_cursor = 0
            self.transition(Phase.EDIT)
        if state.phase == Phase.EDIT:
            # 调用参数和检查描述属于方案契约，先修正方案再继续生成组件。
            diagnostics = propose.invocation_diagnostics(self)
            if diagnostics:
                state.proposal_diagnostics = diagnostics
                state.repair_diagnosis = {
                    'G': {
                        'proposal': [
                            {
                                'kind': 'invalid_proposal_contract',
                                'revision_scope': 'proposal',
                                'repair_requirements': [
                                    'Correct invalid invocation bindings or exact fixture transitions before further code generation. Use explicit public/evidence references or literal JSON values, and preserve valid candidate edits and unchanged plan.'
                                ],
                                'diagnostics': diagnostics,
                            }
                        ]
                    },
                    'L': {},
                }
                self.transition(Phase.PROPOSE)
                return StageResult(state.phase)
            self._implement_edits(public)
            if state.phase == Phase.BLOCKED:
                return StageResult(state.phase, blocked=True)
        if state.phase == Phase.VALIDATE:
            # Validate 的证据绑定整份候选；全 PASS 时将候选交给宿主执行。
            state.validation_evidence = self.validate()
            self.transition(
                Phase.READY
                if all(item['status'] == 'PASS' for item in state.validation_evidence)
                else Phase.REPAIR
            )
        if state.phase == Phase.REPAIR:
            self.repair(public)
        elif state.phase == Phase.PATCH:
            self.patch()
        return StageResult(
            state.phase,
            ready_to_execute=state.phase == Phase.READY,
            blocked=state.phase == Phase.BLOCKED,
        )

    def develop(self, public):
        """Historical boolean entry; the formal result is returned by advance()."""
        return self.advance(public).ready_to_execute

    def _implement_edits(self, public):
        state = self.state
        edits = state.proposal['edits']
        while state.edit_cursor < len(edits):
            edit = edits[state.edit_cursor]
            previous = state.candidate.get(edit['target'])
            # 每个编辑读取其前置版本；多个编辑之间通过 prerequisite_steps 表达顺序。
            expected = previous['hash'] if previous else None
            if edit.get('expected_hash') != expected:
                raise ValueError('Stale edit source')
            completed = {item['id'] for item in edits[: state.edit_cursor]}
            if not set(edit.get('prerequisite_steps', [])) <= completed:
                raise ValueError('Unmet edit prerequisites')
            if edit['operation'] == 'new' and previous:
                raise ValueError('Component name collision')
            delivery = (
                rewrite.rewrite(self, edit, public)
                if previous
                else codegen.generate(self, edit, public)
            )
            if delivery.get('delivery_status') == 'blocked':
                state.blocked_delivery = {
                    'resume_phase': 'edit',
                    'edit_cursor': state.edit_cursor,
                    'record': copy.deepcopy(delivery),
                }
                self.transition(Phase.BLOCKED)
                return
            # 组件交付应用完成后再推进游标，保存的状态对应一项已完成编辑。
            self.apply(delivery, edit['target'], expected)
            state.edit_cursor += 1
            self.save()
        self.transition(Phase.VALIDATE)

    def repair(self, public):
        """按 Reflect 的 revision_scope 选择 Plan、Propose 或组件 Patch。"""
        state = self.state
        state.repair_diagnosis = reflect.reflect(self, public)
        issues = {
            name: values
            for name, values in state.repair_diagnosis.get('G', {}).items()
            if values
        }
        scopes = {issue.get('revision_scope') for values in issues.values() for issue in values}
        # 诊断范围决定回退深度：复用关系、协调方案、单个组件依次对应三个入口。
        if 'reuse_plan' in scopes:
            self.transition(Phase.PLAN)
        elif 'proposal' in scopes or not issues:
            self.transition(Phase.PROPOSE)
        else:
            state.repair_targets = list(issues)
            state.repair_cursor = 0
            self.transition(Phase.PATCH)
            self.patch()

    def patch(self):
        self.transition(patch.patch_components(self))

    def accepted(self, receipt):
        """Compatibility name; callers must supply independent acceptance evidence."""
        return self.accept_task(receipt)

    def maintain(self):
        """Detect a structural opportunity and reuse this same development loop."""
        from .refactor.workflow import maintain

        return maintain(self)

    def activate_task(self, binding, context_profile):
        """Initialize one new task from committed skills; the host archives the old state."""
        self.s = {
            'phase': 'idle',
            'parent': digest(self.library.manifest),
            'candidate': copy.deepcopy(self.library.manifest['components']),
            'p': None,
            'A': {},
            'P': None,
            'E': [],
            'r': None,
            'o': None,
            'history': [],
            'frames': [],
            'pending': None,
            'binding': binding,
            'context_profile': context_profile,
        }

    def prepare_development(self, public):
        """Apply factual repair/replan conditions, then enter the one Develop loop."""
        state = self.state
        # Old checkpoints may predate admission of executable check contracts.
        # Repair the proposal protocol, not otherwise valid candidate source.
        contract_issues = (
            check_contract_diagnostics(state.proposal, self.library.manifest['components'])
            if state.proposal
            else []
        )
        if contract_issues and state.phase not in (Phase.PLAN, Phase.ANALYZE, Phase.PROPOSE):
            state.proposal_diagnostics = contract_issues
            state.repair_diagnosis = {
                'G': {
                    'proposal': [
                        {
                            'kind': 'invalid_check_contract',
                            'revision_scope': 'proposal',
                            'repair_requirements': [
                                'Correct executable check records; preserve valid candidate sources. Use edits=[] if implementation changes are unnecessary.'
                            ],
                            'diagnostics': contract_issues,
                        }
                    ]
                },
                'L': {},
            }
            self.transition(Phase.PROPOSE)
            self.save()
        if state.awaiting_task_verdict:
            state.awaiting_task_verdict = False
            self.transition(Phase.REPAIR)
        if state.needs_replan:
            self.plan(public)
            state.needs_replan = False
            self.save()
        if state.failure and state.phase == Phase.READY:
            self.transition(Phase.REPAIR)
        outcome = self.advance(public)
        if outcome.ready_to_execute and (state.failure or {}).get('type', '').rsplit('.', 1)[
            -1
        ] in (
            'TypeError',
            'ValueError',
            'SyntaxError',
            'KeyError',
            'IndexError',
            'ZeroDivisionError',
            'OverflowError',
        ):
            self.s['last_resolved_development_failure'] = copy.deepcopy(state.failure)
            state.failure = None
        return outcome

    def record_development_failure(self, error):
        """Retain local source/proposal diagnostics; model/provider failures bypass this API."""
        self.state.failure = {'type': type(error).__name__, 'message': str(error)}
        self.transition(Phase.REPAIR)

    def record_execution_failure(self, error):
        """A code failure preserves execution facts; no old entrypoint is restarted."""
        self.state.failure = copy.deepcopy(error)
        invocation = self.state.execution.invocation
        if invocation is not None:
            invocation['status'] = 'failed'
            self.state.execution.recovery = copy.deepcopy(invocation)
        self.transition(Phase.REPAIR)

    def record_task_failure(self, receipt):
        """Independent non-pass evidence drives repair without inventing an operation fault."""
        self.state.failure = copy.deepcopy(receipt)
        self.transition(Phase.REPAIR)

    def record_invocation_issue(self, error):
        """A binding problem revises the proposal before any environment operation."""
        self.s['argument_binding_error'] = {'type': type(error).__name__, 'message': str(error)}
        self.state.repair_diagnosis = {
            'G': {
                'proposal': [
                    {
                        'kind': 'invalid_argument_binding',
                        'revision_scope': 'proposal',
                        'repair_requirements': [str(error)],
                    }
                ]
            },
            'L': {},
        }
        self.transition(Phase.PROPOSE)

    def preserve_legacy_interruption(self):
        """Changing backend does not authorize replaying an old completed prefix."""
        execution = self.state.execution
        if execution.suspended_legacy and not execution.recovery:
            suspended = execution.suspended_legacy
            root = suspended['frames'][0]
            execution.recovery = {
                'invocation_id': root['invocation_id'],
                'component_id': root['component_id'],
                'arguments': copy.deepcopy(root['args']),
                'candidate_hash': suspended['candidate_hash'],
                'status': 'interrupted',
                'legacy_frames': copy.deepcopy(suspended['frames']),
            }

    def prepare_invocation(self, component_id, arguments, invocation_id, continuation_of=None):
        """Validate the exact candidate and explicit input before the host executes it."""
        state = self.state
        recovery = state.execution.recovery
        if state.execution.pending_request:
            raise ValueError('An existing operation still needs reconciliation')
        if recovery and continuation_of != recovery['invocation_id']:
            raise ValueError(
                'An explicit continuation decision must identify the interrupted invocation'
            )
        candidate_hash = digest(state.candidate)
        if not state.validation_evidence or any(
            item.get('status') != 'PASS' or item.get('candidate_hash') != candidate_hash
            for item in state.validation_evidence
        ):
            raise ValueError('Execution requires current candidate checks')
        component = state.candidate[component_id]
        if component.get('language') != python_backend.PROFILE:
            raise ValueError(
                'Use the declared Python callable interface for a normal invocation'
            )
        from jsonschema import Draft202012Validator

        Draft202012Validator(component['interface'].get('args_schema', {})).validate(arguments)
        invocation = {
            'invocation_id': invocation_id,
            'component_id': component_id,
            'arguments': copy.deepcopy(arguments),
            'candidate_hash': candidate_hash,
            'status': 'running',
            'continuation_of': continuation_of,
        }
        state.execution.invocation = invocation
        state.execution.executed_candidate_hash = candidate_hash
        if recovery:
            state.execution.recovery_history.append(copy.deepcopy(recovery))
            state.execution.suspended_legacy = None
        state.execution.recovery = None
        return invocation

    def record_execution_finished(self, result):
        """Component return is execution completion; independent task acceptance is separate."""
        state = self.state
        state.execution.invocation['status'] = 'completed'
        state.execution.last_result = {
            'execution': 'completed',
            'result': result,
            'candidate_hash': state.execution.executed_candidate_hash,
        }
        state.awaiting_task_verdict = True

    def accept_task(self, receipt):
        """将当前候选的任务验收与开发检查汇合，发布声明导出的组件及依赖闭包。"""
        state = self.state
        if receipt['candidate_hash'] != digest(state.candidate):
            raise ValueError('Acceptance for stale candidate')
        # 任务验收先存为事实，后续发布步骤继续使用同一候选版本的开发检查。
        state.accepted_task_receipt = copy.deepcopy(receipt)
        if not state.validation_evidence or any(
            (
                e.get('status') != 'PASS' or e.get('candidate_hash') != digest(state.candidate)
                for e in state.validation_evidence
            )
        ):
            raise ValueError('Publication requires current version-bound passing checks')
        evidence = state.validation_evidence + [
            {
                'id': 'task-evaluator',
                'status': 'PASS',
                'receipt': receipt,
                'candidate_hash': receipt['candidate_hash'],
            }
        ]
        exports = state.proposal.get('library_exports')
        published = state.candidate
        if exports is not None:
            if not isinstance(exports, list) or any(
                (not isinstance(n, str) or n not in state.candidate for n in exports)
            ):
                raise ValueError('Invalid library export selection')
            # 显式导出扩展到依赖闭包，库中其余已提交组件继续沿用。
            closure = set(exports)
            while True:
                expanded = closure | {
                    d for n in closure for d in state.candidate[n]['dependencies']
                }
                if not expanded <= set(state.candidate):
                    raise ValueError('Unresolved exported dependency')
                if expanded == closure:
                    break
                closure = expanded
            published = copy.deepcopy(self.library.manifest['components'])
            published.update({n: state.candidate[n] for n in closure})
        previous = self.library.manifest['components']
        delta = {
            'changed': [n for n, c in published.items() if previous.get(n) != c],
            'removed': [n for n in previous if n not in published],
        }
        commit = self.library.commit(
            state.base_library_hash,
            published,
            evidence,
            state.proposal,
            validated_candidate=state.candidate,
        )
        state.publication_delta = delta
        state.published_commit = commit
        self.transition(Phase.DETECT)
        return commit
