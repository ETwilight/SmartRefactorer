"""Run a candidate Python function and exchange its environment requests with the adapter."""

import asyncio
import copy
import dis
import inspect
import queue
import sys
import threading
import traceback
import uuid
from .python_backend import SkillModules, bind


def type_name(value):
    """Return the Python type name qualified by its defining module."""

    kind = type(value)
    return (
        type.__getattribute__(kind, '__module__')
        + '.'
        + type.__getattribute__(kind, '__qualname__')
    )


def snapshot(value, seen=None):
    """Convert a value into a JSON record for the execution journal.

    JSON scalars and containers retain their values. Tuples and sets carry type
    labels; other objects carry their type and identity. seen tracks the current
    recursion path so cyclic references can be represented by an index.
    """
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        import math

        return value if math.isfinite(value) else {'python_type': 'float', 'repr': repr(value)}
    # seen: None | dict[int, int] -> dict[int, int], mapping object identity to recursion index.
    seen = {} if seen is None else seen
    identity = id(value)
    if identity in seen:
        return {'python_reference': seen[identity]}
    seen[identity] = len(seen)
    try:
        if type(value) is list:
            return [snapshot(item, seen) for item in value]
        if type(value) is dict and all(type(key) is str for key in value):
            return {key: snapshot(item, seen) for key, item in value.items()}
        if type(value) in (tuple, set, frozenset):
            return {
                'python_type': type(value).__name__,
                'items': [snapshot(item, seen) for item in value],
            }
        return {'python_type': type_name(value), 'opaque': True, 'object_identity': identity}
    finally:
        seen.pop(identity, None)


def exception_snapshot(exc):
    """Collect the exception type, arguments, message, and formatted traceback."""

    try:
        message = str(exc)
    except BaseException:
        message = '<exception message formatting failed>'
    try:
        formatted = ''.join(traceback.format_exception(exc))
    except BaseException:
        formatted = ''.join(traceback.format_tb(exc.__traceback__)) + type_name(exc)
    return {
        'type': type_name(exc),
        'message': message,
        'args': snapshot(exc.args),
        'traceback': formatted,
    }


class Program:
    """Execute one candidate call in a worker thread.

    components is a component-ID mapping copied for this invocation. entry names
    the component to call, arguments supplies its input, and directory holds the
    temporary Python modules loaded by SkillModules. result keeps the actual Python
    return value; exception keeps a raised exception object.

    events is a Queue of dictionaries sent to the adapter. A request event suspends
    the calling skill until the adapter places its feedback in answers. completed
    and exception events finish the invocation. cancel() releases a waiting request.

    trace_calls adds function-boundary events. trace_values also snapshots helper
    arguments and returns; these values can be large when they include world data.

    Example, with call tracing disabled to show just request exchange::

        program = Program(
            components, entry, arguments, directory, trace_calls=False
        ).start()
        while True:
            event = program.events.get()
            if event['kind'] != 'request':
                break
            program.deliver(environment.handle(event['request']))

    The final event contains either the component result or its exception record.
    """

    def __init__(
        self,
        components,
        entry,
        arguments,
        directory,
        invocation_id=None,
        *,
        trace_calls=True,
        trace_values=False
    ):
        """Copy the invocation inputs and create its request and feedback queues."""

        self.components = copy.deepcopy(components)
        self.entry = entry
        self.arguments = copy.deepcopy(arguments)
        self.directory = directory
        self.invocation_id = invocation_id or str(uuid.uuid4())
        self.events = queue.Queue()
        self.answers = queue.Queue()
        self.cancelled = threading.Event()
        self.serial = threading.Lock()
        self.thread = None
        self.exception = None
        self.result = None
        # Root arguments, results, requests, and exceptions have their own journal records.

        self.trace_calls = trace_calls
        self.trace_values = trace_values

    def start(self):
        """Start the worker thread and return this Program."""

        self.thread = threading.Thread(
            target=self._run, name='sr-program-' + self.invocation_id, daemon=True
        )
        self.thread.start()
        return self

    def request(self, request):
        """Send one public request and wait for the adapter feedback."""

        if not isinstance(request, dict):
            raise TypeError('The request capability accepts a public request dictionary')
        with self.serial:
            if self.cancelled.is_set():
                raise InterruptedError('Execution stopped; no request dispatched')
            self.events.put({'kind': 'request', 'request': copy.deepcopy(request)})
            while not self.cancelled.is_set():
                try:
                    return self.answers.get(timeout=0.1)
                except queue.Empty:
                    pass
            raise InterruptedError(
                'Execution stopped while retaining the original pending request'
            )

    async def request_async(self, request):
        """Run the blocking request exchange through an asyncio worker thread."""

        return await asyncio.to_thread(self.request, request)

    def deliver(self, feedback):
        """Place a copy of the adapter feedback in the waiting call queue."""

        self.answers.put(copy.deepcopy(feedback))

    def cancel(self):
        """Signal the worker to stop waiting for feedback."""

        self.cancelled.set()

    def _run(self):
        """Load candidate modules, call the entrypoint, and publish its final event."""

        try:
            with SkillModules(self.components, self.directory) as modules:
                source_files = {str(path): name[7:] for name, path in modules.files.items()}
                # instructions: dict[CodeType, dict[int, tuple[str, str]]], indexed by bytecode
                # offset.
                instructions = {}
                # active_frames: set[int] of frame identities whose entry has already been
                # recorded.
                active_frames = set()
                public_names = {
                    name: spec.get('interface', {}).get('entrypoint', 'run')
                    for name, spec in self.components.items()
                }

                def profile(frame, event, value):
                    """Record selected skill calls and returns, including generator
                    suspension.
                    """

                    name = source_files.get(frame.f_code.co_filename)
                    if name is None or frame.f_code.co_name == '<module>':
                        return
                    identity = id(frame)
                    if event == 'call':
                        # Resuming a generator emits another call event for the same open frame.

                        if identity in active_frames:
                            return
                        caller = (
                            source_files.get(frame.f_back.f_code.co_filename)
                            if frame.f_back
                            else None
                        )
                        if not self.trace_values:
                            if frame.f_code.co_name in (
                                '<genexpr>',
                                '<listcomp>',
                                '<dictcomp>',
                                '<setcomp>',
                            ):
                                return
                            if (
                                caller == name
                                and frame.f_code.co_qualname != public_names[name]
                            ):
                                return
                        active_frames.add(identity)
                        boundary = {
                            'kind': 'function.enter',
                            'component_id': name,
                            'function': frame.f_code.co_qualname,
                            'line': frame.f_lineno,
                            'values_recorded': self.trace_values,
                        }
                        if self.trace_values:
                            boundary['arguments'] = snapshot(frame.f_locals)
                        else:
                            boundary['argument_names'] = list(
                                frame.f_code.co_varnames[
                                    : frame.f_code.co_argcount + frame.f_code.co_kwonlyargcount
                                ]
                            )
                        self.events.put(boundary)
                    elif event == 'return':
                        if identity not in active_frames:
                            return
                        if frame.f_code not in instructions:
                            previous = ''
                            decoded = {}
                            for instruction in dis.get_instructions(frame.f_code):
                                decoded[instruction.offset] = (instruction.opname, previous)
                                previous = instruction.opname
                            instructions[frame.f_code] = decoded
                        opcodes = instructions[frame.f_code]
                        opcode, previous = opcodes.get(frame.f_lasti, ('', ''))
                        suspended = opcode == 'YIELD_VALUE' or (
                            opcode == 'RESUME' and previous == 'YIELD_VALUE'
                        )
                        if suspended and not self.trace_values:
                            return
                        if not suspended:
                            active_frames.discard(identity)
                        kind = (
                            'function.return'
                            if opcode.startswith('RETURN_')
                            else 'function.suspend' if suspended else 'function.exit'
                        )
                        boundary = {
                            'kind': kind,
                            'component_id': name,
                            'function': frame.f_code.co_qualname,
                            'line': frame.f_lineno,
                            'values_recorded': self.trace_values,
                        }
                        if self.trace_values:
                            boundary['value'] = snapshot(value)
                        self.events.put(boundary)

                function = modules.entry(self.entry, self.components[self.entry]['interface'])
                args, kwargs = bind(
                    function,
                    self.arguments,
                    self.components[self.entry]['interface'],
                    {'request': self.request, 'request_async': self.request_async},
                )
                if self.trace_calls:
                    sys.setprofile(profile)
                try:
                    self.result = function(*args, **kwargs)
                    if inspect.isawaitable(self.result):
                        # self.result: Awaitable[Any] -> Any, the completed async skill return
                        # value.
                        self.result = asyncio.run(self.result)
                finally:
                    if self.trace_calls:
                        sys.setprofile(None)
            self.events.put({'kind': 'completed', 'result': snapshot(self.result)})
        except BaseException as exc:
            self.exception = exc
            self.events.put({'kind': 'exception', 'error': exception_snapshot(exc)})
        finally:
            self.cancelled.set()
