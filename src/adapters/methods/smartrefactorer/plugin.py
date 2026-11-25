"""Construct the SR method and task runner from a configured run context."""

from runtime.task_session import SessionRunner
from .policy import MethodAdapter
from .checkpoint import ContinuationService


class Plugin:
    """Create the SR adapter and runner used by the method registry.

    run() receives a RunContext containing the environment, output layout, task
    goal, token limits, and method options. It configures the embedding backend and
    continuation source, then runs SessionRunner with MethodAdapter.

    Example, after the worker has prepared its RunContext::

        report = Plugin().run(context)

    report contains the task rounds, model usage, and method summary from the run.
    """

    def run(self, context):
        """Build the configured method adapter and return the task runner's report."""

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
