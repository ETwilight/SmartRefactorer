"""Immutable environment task selection, shared by every workflow on a port."""

import copy
import json
from pathlib import Path

if __package__:
    from .task_catalog import list_tasks, stable_hash, DIFFICULTIES
else:
    from task_catalog import list_tasks, stable_hash, DIFFICULTIES


class TaskSelection:
    def __init__(self, selection=None, world_type=None):
        catalogue = list_tasks()
        known = {t['id']: t for t in catalogue}
        if isinstance(selection, dict):
            selection = copy.deepcopy(selection)
            declared_world = selection.pop('world_type', None)
            if (
                world_type is not None
                and declared_world is not None
                and world_type != declared_world
            ):
                raise ValueError('Task selection world type does not match the server')
            world_type = world_type or declared_world
        if world_type is not None and world_type not in ('void', 'natural'):
            raise ValueError('Unknown world type: ' + str(world_type))
        if selection is None or selection == 'core':
            selection = json.loads(
                (
                    Path(__file__).resolve().parents[2] / 'assets/techmc/tasks/core.json'
                ).read_text(encoding='utf-8')
            )
        if selection == 'all':
            selection = {'mode': 'all'}
        elif isinstance(selection, str):
            selection = {'difficulties': [selection]}
        elif isinstance(selection, list):
            selection = (
                {'difficulties': selection}
                if all(x in DIFFICULTIES for x in selection)
                else {'task_ids': selection}
            )
        if not isinstance(selection, dict):
            raise ValueError('Task selection must be core, all, difficulties, or task_ids')
        mode = selection.get('mode') or (
            'difficulties'
            if set(selection) == {'difficulties'}
            else 'custom' if set(selection) == {'task_ids'} else None
        )
        if mode == 'all' and set(selection) == {'mode'}:
            spec = {'mode': 'all'}
            selected = catalogue
        elif mode == 'difficulties' and not set(selection) - {'mode', 'difficulties'}:
            levels = selection.get('difficulties')
            if (
                not isinstance(levels, list)
                or not levels
                or any(not isinstance(x, str) or x not in DIFFICULTIES for x in levels)
            ):
                raise ValueError(
                    'difficulties must be a nonempty list of easy, medium, hard and/or expert'
                )
            levels = [x for x in DIFFICULTIES if x in levels]
            spec = {'mode': mode, 'difficulties': levels}
            selected = [t for t in catalogue if t['difficulty'] in levels]
        elif mode == 'custom' and not set(selection) - {'mode', 'task_ids'}:
            ids = selection.get('task_ids')
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(x, str) or x not in known for x in ids)
            ):
                raise ValueError('task_ids must be a nonempty list of published task IDs')
            selected = [t for t in catalogue if t['id'] in ids]
            spec = {'mode': mode, 'task_ids': [t['id'] for t in selected]}
        else:
            raise ValueError('Invalid or mixed task selection fields')
        if world_type is not None:
            if mode == 'custom' and any(
                t.get('world_type', 'void') != world_type for t in selected
            ):
                raise ValueError('Selected task requires another world type')
            selected = [t for t in selected if t.get('world_type', 'void') == world_type]
            spec['world_type'] = world_type
        self.spec = spec
        self._tasks = selected
        self._by_id = {t['id']: t for t in selected}
        self.catalogue_count = len(catalogue)
        self.selection_hash = stable_hash(
            {
                'selection': spec,
                'tasks': [{k: t[k] for k in ('id', 'task_hash')} for t in selected],
            }
        )

    def tasks(self, difficulty=None, world_type=None):
        if difficulty is not None and difficulty not in DIFFICULTIES:
            raise ValueError('Unknown difficulty: ' + str(difficulty))
        if world_type is not None and world_type not in ('void', 'natural'):
            raise ValueError('Unknown world type: ' + str(world_type))
        return copy.deepcopy(
            [
                t
                for t in self._tasks
                if (difficulty is None or t['difficulty'] == difficulty)
                and (world_type is None or t.get('world_type', 'void') == world_type)
            ]
        )

    def require(self, task_id):
        if not isinstance(task_id, str) or task_id not in self._by_id:
            raise ValueError(
                'Task is unknown or outside the active environment task selection: '
                + str(task_id)
            )

    def get(self, task_id):
        self.require(task_id)
        return copy.deepcopy(self._by_id[task_id])

    @property
    def metadata(self):
        return {
            **copy.deepcopy(self.spec),
            'task_count': len(self._tasks),
            'catalogue_task_count': self.catalogue_count,
            'selection_hash': self.selection_hash,
            'immutable_for_session': True,
        }


def managed_selection(root, port):
    """Read the selected local/SSH managed server; no game mutation."""
    if port is None:
        return None, 'default'
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid task selection server port')
    if __package__:
        from .remote_host import for_port
    else:
        from remote_host import for_port
    remote = for_port(port, root)
    if remote:
        state = remote.state(fresh=True)
        if not state:
            raise RuntimeError(
                'Remote server has no managed instance; start it before creating Environment'
            )
    else:
        path = Path(root) / 'runtime/servers' / str(port) / 'current.json'
        if not path.exists():
            return None, 'default'
        state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('status') not in ('starting', 'ready'):
        return None, 'default'
    if state.get('bridge_port') != port:
        raise ValueError('Task selection configuration belongs to another server port')
    return (
        TaskSelection(
            state.get('task_selection'), world_type=state.get('world_type', 'void')
        ).spec,
        'managed_server_port',
    )


def add_selection_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--task-list',
        nargs='+',
        choices=['core', 'all', *DIFFICULTIES],
        help='core (default), all, or one or several difficulties',
    )
    group.add_argument('--task-ids', nargs='+', help='Custom task IDs, across any difficulties')
    group.add_argument(
        '--task-selection-file',
        type=Path,
        help='UTF-8 JSON: core, all, a difficulty list, or a task_ids object',
    )


def selection_from_args(args):
    levels = getattr(args, 'task_list', None)
    if levels:
        if 'core' in levels:
            if levels != ['core']:
                raise ValueError('core cannot be combined with other task lists')
            return 'core'
        if 'all' in levels:
            if levels != ['all']:
                raise ValueError('all cannot be combined with difficulty names')
            return 'all'
        return {'difficulties': levels}
    ids = getattr(args, 'task_ids', None)
    if ids is not None:
        return {'task_ids': ids}
    path = getattr(args, 'task_selection_file', None)
    if path is not None:
        value = Path(path).read_bytes()
        if len(value) > 65536:
            raise ValueError('Task selection file exceeds 64 KiB')
        result = json.loads(value.decode('utf-8-sig'))
        if result is None:
            raise ValueError('Task selection file must specify all, difficulties, or task_ids')
        return result
    return None
