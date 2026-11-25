"""Order task visits, reveal arrived task definitions, and persist host-reported visit
outcomes.
"""

import copy
import json
import threading
from pathlib import Path

if __package__:
    from .task_catalog import stable_hash
else:
    from task_catalog import stable_hash


def ordered_tasks(tasks, sequence='curriculum'):
    """Order task definitions by curriculum, interleaved difficulty, or separated family
    returns.

    Family returns connect tasks through selected prerequisites and shared
    primary products, then take one task from each family per pass.
    """

    tasks = sorted(tasks, key=lambda t: t.get('curriculum', {}).get('order', 9999))
    if sequence == 'curriculum':
        return tasks
    if sequence not in ('interleaved', 'family_return'):
        raise ValueError('Unknown task sequence')
    levels = ('easy', 'medium', 'hard', 'expert')
    groups = [[t for t in tasks if t['difficulty'] == level] for level in levels]
    if sequence == 'interleaved':
        return [
            group[i]
            for i in range(max(map(len, groups), default=0))
            for group in groups
            if i < len(group)
        ]
    # Round-robin the related production families to separate their later visits in time.
    families = {}
    # roots: dict[str, str], the parent links joining prerequisite- and product-related task
    # families.
    roots = {t['id']: t['id'] for t in tasks}

    def root(identifier):
        """Follow the family-parent mapping to its representative task ID."""

        while roots[identifier] != identifier:
            identifier = roots[identifier]
        return identifier

    # products: dict[str, str], mapping each seen product to its most recently encountered task
    # ID.
    products = {}
    for task in tasks:
        related = [x for x in task.get('curriculum', {}).get('prerequisites', []) if x in roots]
        for product in task.get('primary_products', []):
            if product in products:
                related.append(products[product])
            products[product] = task['id']
        for identifier in related:
            roots[root(task['id'])] = root(identifier)
    for task in tasks:
        families.setdefault(root(task['id']), []).append(task)
    # groups now contains production-family buckets; the earlier groups variable held difficulty
    # buckets.
    groups = list(families.values())
    return [
        group[i]
        for i in range(max(map(len, groups), default=0))
        for group in groups
        if i < len(group)
    ]


class TaskStream:
    """Deliver a configured sequence of task visits and retain completed outcomes.

    selection supplies copied task definitions. sequence chooses a generated
    order; explicit task_ids supplies arrival order and permits repeated IDs.
    _tasks is the visit queue, position is its zero-based current index, and
    history contains one task_id/outcome record per completed visit. A deferred
    outcome appends a copy of the current task to the end of the queue.

    manifest records the original task IDs, definition hashes, and ordering
    hash. state_path is Path | None; when present, construction restores the
    matching manifest, position, and history, reconstructing deferred visits.
    _lock serializes advancement. world_type identifies the selected world type
    when it is fixed by the selection or shared by all tasks.

    visible() returns unique arrived task definitions with future curriculum
    references removed from their copied metadata. public_state() exposes the
    current visit and completed history. The host calls advance() with a visit
    outcome and persists that advancement before changing the in-memory state.

    Example:
        stream = TaskStream(selection, state_path=state_path)
        task_id = stream.current_id
        stream.advance('deferred')

    task_id is the task offered by the first visit. Deferring it completes that
    visit and queues another visit to the same task after the remaining arrivals.
    """

    def __init__(self, selection, sequence='curriculum', task_ids=None, state_path=None):
        """Build the arrival queue and restore persisted position, outcomes, and deferred
        visits when available.
        """

        tasks = selection.tasks()
        if task_ids is not None:
            if (
                not isinstance(task_ids, list)
                or not task_ids
                or any(not isinstance(x, str) for x in task_ids)
            ):
                raise ValueError('Task sequence must contain task IDs in arrival order')
            tasks = [selection.get(task_id) for task_id in task_ids]
            sequence = 'custom'
        else:
            tasks = ordered_tasks(tasks, sequence)
        self._tasks = tasks
        self.position = 0
        # history: list[dict], with task_id and outcome for each completed visit.
        self.history = []
        self._lock = threading.RLock()
        self.state_path = Path(state_path) if state_path is not None else None
        worlds = {t.get('world_type', 'void') for t in tasks}
        self.world_type = selection.spec.get('world_type') or (
            next(iter(worlds)) if len(worlds) == 1 else None
        )
        self.manifest = {
            'protocol': 'sequential_hidden_v1',
            'sequence': sequence,
            'task_ids': [t['id'] for t in tasks],
            'task_hashes': [t['task_hash'] for t in tasks],
        }
        self.manifest['sequence_hash'] = stable_hash(self.manifest)
        if self.state_path is not None:
            if self.state_path.exists():
                state = json.loads(self.state_path.read_text(encoding='utf-8'))
                if state.get('manifest') != self.manifest:
                    raise ValueError('Persisted task stream configuration changed')
                position = state.get('position')
                history = state.get('history')
                if (
                    not isinstance(position, int)
                    or isinstance(position, bool)
                    or position < 0
                    or not isinstance(history, list)
                    or position != len(history)
                ):
                    raise ValueError('Invalid persisted task stream position/history')
                # restored: list[dict], rebuilt from the original arrivals plus deferred visits
                # found in saved history.
                restored = copy.deepcopy(tasks)
                for index, entry in enumerate(history):
                    if index >= len(restored) or entry.get('task_id') != restored[index]['id']:
                        raise ValueError(
                            'Persisted task stream history does not match arrivals'
                        )
                    if entry.get('outcome') not in self.OUTCOMES:
                        raise ValueError('Invalid persisted task stream outcome')
                    if entry['outcome'] == 'deferred':
                        restored.append(copy.deepcopy(restored[index]))
                if position > len(restored):
                    raise ValueError('Task stream position exceeds arrivals')
                self._tasks = restored
                self.position = position
                self.history = history
            else:
                self._persist(0, [])

    OUTCOMES = (
        'pass',
        'failed',
        'deferred',
        'attempt_budget_exhausted',
        'task_token_budget_exhausted',
        'time_budget_exhausted',
        'budget_exhausted',
    )

    def _persist(self, position, history):
        """Write the manifest, next position, and outcome history when a state path is
        configured.
        """

        if self.state_path is None:
            return
        if __package__:
            from .runtime_support import atomic_json
        else:
            from runtime_support import atomic_json
        atomic_json(
            self.state_path,
            {
                'schema_version': 1,
                'manifest': self.manifest,
                'position': position,
                'history': history,
            },
        )

    @property
    def current_id(self):
        """Return the current visit's task ID, or None when the queue is complete."""

        return self._tasks[self.position]['id'] if self.position < len(self._tasks) else None

    def require(self, task_id, current=False):
        """Check that a task has arrived, optionally requiring it to be the current visit."""

        allowed = [t['id'] for t in self._tasks[: self.position + 1]]
        if task_id not in allowed or (current and task_id != self.current_id):
            raise ValueError('Task is unavailable in the current task stream')

    def visible(self, difficulty=None, world_type=None):
        """Return distinct arrived tasks as filtered copies with future curriculum references
        removed.
        """
        # Return one catalogue entry per arrived task ID, even when that task has multiple
        # visits.

        tasks = []
        seen = set()
        for task in self._tasks[: min(self.position + 1, len(self._tasks))]:
            if task['id'] not in seen:
                tasks.append(copy.deepcopy(task))
                seen.add(task['id'])
        for task in tasks:
            self.redact(task)
        return [
            t
            for t in tasks
            if (difficulty is None or t['difficulty'] == difficulty)
            and (world_type is None or t.get('world_type', 'void') == world_type)
        ]

    def redact(self, task):
        """Remove unrevealed prerequisite and dependency IDs from the supplied task's
        curriculum metadata.
        """
        revealed = {t['id'] for t in self._tasks[: self.position + 1]}
        curriculum = task.get('curriculum', {})
        for key in ('prerequisites', 'dependencies'):
            if key in curriculum:
                curriculum[key] = [x for x in curriculum[key] if x in revealed]
        return task

    def get(self, task_id):
        """Return the visible copied definition for an arrived task ID."""

        self.require(task_id)
        return next(t for t in self.visible() if t['id'] == task_id)

    def public_state(self):
        """Return the current visit, world type, completion status, and copied outcome
        history.
        """

        return {
            'protocol': 'sequential_hidden_v1',
            'current_task_id': self.current_id,
            'world_type': self.world_type,
            'position': self.position,
            'visit': self.position + 1 if self.current_id is not None else None,
            'revealed_tasks': len({t['id'] for t in self._tasks[: self.position + 1]}),
            'complete': self.current_id is None,
            'history': copy.deepcopy(self.history),
        }

    def advance(self, outcome):
        """Persist the host-reported outcome, append deferred work when needed, and move to
        the next visit.
        """
        if outcome not in self.OUTCOMES:
            raise ValueError('Unknown task stream outcome')
        with self._lock:
            if self.current_id is None:
                return
            # Build a new history list so persistence precedes mutation of the current stream
            # state.
            history = self.history + [{'task_id': self.current_id, 'outcome': outcome}]
            # Persist the next position and outcome before updating in-memory progress.
            self._persist(self.position + 1, history)
            if outcome == 'deferred':
                self._tasks.append(copy.deepcopy(self._tasks[self.position]))
            self.history = history
            self.position += 1


def add_stream_arguments(parser):
    """Add generated-order and explicit task-sequence-file CLI options."""

    parser.add_argument(
        '--task-sequence',
        choices=['curriculum', 'interleaved', 'family_return'],
        default='curriculum',
    )
    parser.add_argument(
        '--task-sequence-file',
        type=Path,
        help='Researcher-only JSON array of task IDs in arrival order',
    )


def stream_from_args(args):
    """Build stream configuration from CLI ordering options and the optional JSON arrival
    list.
    """

    return {
        'sequence': args.task_sequence,
        'task_ids': (
            json.loads(args.task_sequence_file.read_text(encoding='utf-8-sig'))
            if args.task_sequence_file
            else None
        ),
    }
