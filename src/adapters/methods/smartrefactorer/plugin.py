"""Register the formal algorithm through its method adapter and generic host."""

from runtime.task_session import SessionRunner
from .policy import MethodAdapter
from .checkpoint import ContinuationService


class Plugin:
    def run(self, context):
        options = context.options or {}
        source = options.get('smartrefactorer_resume_from')
        return SessionRunner(
            context.environment,
            MethodAdapter(
                source,
                context.environment,
                embedding_device=options.get('bge_device', 'cuda:0'),
                embedding_precision=options.get('bge_precision', 'float32'),
            ),
            max_rounds=context.max_rounds,
            token_budget=context.total_tokens,
            round_token_budget=context.round_tokens,
            output_root=context.layout.root.parent.parent,
            workflow='smartrefactorer',
            auditor_factory=options.get('auditor_factory'),
            compact_discovery=True,
            resume_from=source,
            continuation_service=ContinuationService(),
        ).run(context.goal)
