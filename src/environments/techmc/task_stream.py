"""Experiment-owned task delivery. Advancement is deliberately not an agent RPC."""

import copy
import json
import threading
from pathlib import Path

if __package__:
    from .task_catalog import stable_hash
else:
    from task_catalog import stable_hash


def ordered_tasks(tasks, sequence='curriculum'):
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
    # Separate related production families in time to measure delayed reuse.
    families = {}
    roots = {t['id']: t['id'] for t in tasks}

    def root(identifier):
        while roots[identifier] != identifier:
            identifier = roots[identifier]
        return identifier

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
    groups = list(families.values())
    return [
        group[i]
        for i in range(max(map(len, groups), default=0))
        for group in groups
        if i < len(group)
    ]


class TaskStream:
    def __init__(self, selection, sequence='curriculum', task_ids=None, state_path=None):
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
        return self._tasks[self.position]['id'] if self.position < len(self._tasks) else None

    def require(self, task_id, current=False):
        allowed = [t['id'] for t in self._tasks[: self.position + 1]]
        if task_id not in allowed or (current and task_id != self.current_id):
            raise ValueError('Task is unavailable in the current task stream')

    def visible(self, difficulty=None, world_type=None):
        # A repeated arrival is a visit, not a duplicated catalogue entry.
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
        """Project delivery metadata only; canonical definitions/hashes stay fixed."""
        revealed = {t['id'] for t in self._tasks[: self.position + 1]}
        curriculum = task.get('curriculum', {})
        for key in ('prerequisites', 'dependencies'):
            if key in curriculum:
                curriculum[key] = [x for x in curriculum[key] if x in revealed]
        return task

    def get(self, task_id):
        self.require(task_id)
        return next(t for t in self.visible() if t['id'] == task_id)

    def public_state(self):
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
        """Trusted host reports its outcome; this layer never owns model budgets."""
        if outcome not in self.OUTCOMES:
            raise ValueError('Unknown task stream outcome')
        with self._lock:
            if self.current_id is None:
                return
            history = self.history + [{'task_id': self.current_id, 'outcome': outcome}]
            # Publish durable advancement before mutating any in-memory state.
            self._persist(self.position + 1, history)
            if outcome == 'deferred':
                self._tasks.append(copy.deepcopy(self._tasks[self.position]))
            self.history = history
            self.position += 1


def add_stream_arguments(parser):
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
    return {
        'sequence': args.task_sequence,
        'task_ids': (
            json.loads(args.task_sequence_file.read_text(encoding='utf-8-sig'))
            if args.task_sequence_file
            else None
        ),
    }
