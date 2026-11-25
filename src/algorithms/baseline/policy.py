"""Choose requests from recent decision feedback and an explicitly maintained working-memory
string.
"""

import copy
from pathlib import Path


class Policy:
    """Generate request, remember, or finish decisions from a bounded recent history.

    model accepts instructions, context, schema, and scope and returns a
    (decision, metadata) tuple. save writes memory records and clock supplies
    their timestamps. instructions and decision_schema define the model turn.
    memory is the full working-memory str; requests is a list of model-call
    metadata dictionaries, despite its name.

    bind() selects the memory file for a run. decide() sends the last six
    decision/feedback entries, the total history length, and memory to the
    model. A remember decision replaces memory with its report text and saves
    that text when a run directory has been bound.

    Example:
        policy = Policy(model, save_json, instructions, decision_schema, utc_now)
        policy.bind(run_folder, recorder)
        decision = policy.decide(context)

    decision is returned to the task runner for dispatch or episode completion;
    a remember decision updates the memory supplied to later decisions.
    """

    compact_transport = True

    def __init__(self, model, save, instructions, decision_schema, clock):
        """Bind the model, memory writer, instructions, response schema, and timestamp
        source.
        """

        self.model = model
        self.save = save
        self.clock = clock
        self.instructions = instructions
        self.decision_schema = decision_schema
        self.requests = []
        self.memory = ''

    def bind(self, folder, recorder):
        """Select this run's working-memory file."""

        self.memory_file = Path(folder) / 'method/state/working-memory.json'

    def decide(self, context):
        """Generate one decision, apply a remember update, and retain its model-call
        metadata.
        """
        # Supply the last six decisions and feedback; older results remain available through
        # feedback.list/get.

        context = {
            **context,
            'history': context['history'][-6:],
            'working_memory': self.memory,
            'history_access': {
                'retained_decisions': 6,
                'original_decisions': context.get('history_offset', 0)
                + len(context['history']),
                'older_results': 'feedback.list/get',
            },
        }
        # decision: dict matching decision_schema; meta: dict describing this model call.
        decision, meta = self.model(
            self.instructions,
            context,
            self.decision_schema,
            'baseline_decision',
            max_output_tokens=32768,
        )
        if decision['decision'] == 'remember':
            # remember replaces the full working-memory string used by subsequent decisions.
            self.memory = decision['report']
            if hasattr(self, 'memory_file'):
                self.save(
                    self.memory_file,
                    {
                        'source': 'agent_written',
                        'text': self.memory,
                        'updated_at': self.clock(),
                    },
                )
        self.requests.append(meta)
        return decision
