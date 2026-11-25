"""Parse skill source, load candidate modules, bind calls, and run recorded fixtures."""

import ast
import asyncio
import importlib
import importlib.abc
import importlib.util
import inspect
import sys
from pathlib import Path
from ..library import digest

PROFILE = 'python-v1'


def parse(source):
    """Parse and compile source to check its syntax, returning the AST."""

    tree = ast.parse(source)
    compile(tree, '<skill>', 'exec')
    return tree


def references(source, component_id=None):
    """Find imported skill IDs and call sites, and flag unresolved dynamic imports."""
    tree = parse(source)
    dependencies = set()
    dynamic = False
    aliases = {}
    direct_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('skills.'):
                    dependencies.add(alias.name[7:])
                    aliases[alias.asname or alias.name] = alias.name[7:]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if node.level:
                package = 'skills' + (
                    '.' + component_id.rpartition('.')[0]
                    if component_id and '.' in component_id
                    else ''
                )
                try:
                    module = importlib.util.resolve_name('.' * node.level + module, package)
                except ImportError:
                    dynamic = True
            if module == 'skills':
                for alias in node.names:
                    if alias.name == '*':
                        dynamic = True
                        continue
                    dependencies.add(alias.name)
                    aliases[alias.asname or alias.name] = alias.name
            elif module.startswith('skills.'):
                dependencies.add(module[7:])
                for alias in node.names:
                    if alias.name == '*':
                        dynamic = True
                    else:
                        aliases[alias.asname or alias.name] = module[7:]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'import_module'
        ):
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith('skills.')
            ):
                dependencies.add(node.args[0].value[7:])
            else:
                dynamic = True
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ('__import__', 'eval', 'exec')
        ):
            dynamic = True

    def dotted(node):
        """Join a name or attribute chain into its dotted Python spelling."""

        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return dotted(node.value) + '.' + node.attr
        return ''

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = dotted(node.func)
            for alias, component in aliases.items():
                if function == alias or function.startswith(alias + '.'):
                    direct_calls.add(component)
    return {
        'calls': sorted(dependencies),
        'call_sites': sorted(direct_calls),
        'unresolved_dynamic_calls': dynamic,
    }


def bind(function, arguments, interface, capabilities):
    """Bind declared inputs and supplied host callbacks to the function signature."""

    style = interface.get('call_style', 'kwargs')
    if style == 'kwargs':
        positional = []
        keywords = dict(arguments)
    elif style == 'args':
        positional = list(arguments)
        keywords = {}
    elif style == 'value':
        positional = [arguments]
        keywords = {}
    else:
        raise ValueError('Unknown call_style: ' + str(style))
    signature = inspect.signature(function)
    # bound: inspect.BoundArguments, later completed with host callbacks and default arguments.
    bound = signature.bind_partial(*positional, **keywords)
    for parameter, capability in interface.get('host_bindings', {}).items():
        if parameter in bound.arguments:
            raise ValueError('Host binding would overwrite explicit argument: ' + parameter)
        if parameter not in signature.parameters:
            raise ValueError('Host binding names no parameter: ' + parameter)
        if capability not in capabilities:
            raise ValueError('Unknown declared host capability: ' + str(capability))
        bound.arguments[parameter] = capabilities[capability]
    bound.apply_defaults()
    signature.bind(*bound.args, **bound.kwargs)
    return bound.args, bound.kwargs


class SkillModules(importlib.abc.MetaPathFinder):
    """Load candidate components as modules under the skills package.

    components maps component IDs to source records. directory holds one generated
    Python file per component; files maps each qualified module name to that Path.
    Entering the context saves existing skills modules and installs this finder.
    Exiting removes the candidate modules and restores the saved module objects.

    Example, after preparing a candidate::

        with SkillModules(components, directory) as modules:
            function = modules.entry(component_id, components[component_id]['interface'])

    function is the declared callable from this candidate's module set. Calls and
    imports during the context use the same candidate versions.
    """

    def __init__(self, components, directory):
        """Keep the candidate sources and the directory used for generated modules."""

        self.components = components
        self.directory = Path(directory)
        self.files = {}

    def __enter__(self):
        """Write the source files and install the candidate module finder."""

        self.directory.mkdir(parents=True, exist_ok=True)
        for name, component in self.components.items():
            path = self.directory / (digest(name) + '.py')
            path.write_text(component['source'], encoding='utf-8')
            self.files['skills.' + name] = path
        # Save the process module objects before loading this candidate under the same skills
        # names.
        self.previous = {
            name: module
            for name, module in sys.modules.items()
            if name == 'skills' or name.startswith('skills.')
        }
        for name in self.previous:
            sys.modules.pop(name)
        sys.meta_path.insert(0, self)
        return self

    def find_spec(self, fullname, path=None, target=None):
        """Return a module or package specification for a candidate skill name."""

        if fullname == 'skills':
            spec = importlib.util.spec_from_loader(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = []
            return spec
        if fullname in self.files:
            return importlib.util.spec_from_file_location(fullname, self.files[fullname])
        if fullname.startswith('skills.'):
            if any(name.startswith(fullname + '.') for name in self.files):
                spec = importlib.util.spec_from_loader(fullname, loader=None, is_package=True)
                spec.submodule_search_locations = []
                return spec
            raise ModuleNotFoundError('No candidate component ' + fullname[7:])
        return None

    def entry(self, component_id, interface):
        """Import a component and resolve the callable named by its interface."""
        # function: ModuleType initially; entrypoint resolution below changes it to
        # Callable[..., Any].

        function = importlib.import_module('skills.' + component_id)
        entrypoint = interface.get('entrypoint')
        if not isinstance(entrypoint, str) or not entrypoint:
            raise ValueError('Python interface requires its actual entrypoint')
        for name in entrypoint.split('.'):
            # Each attribute can be a namespace or class; the last one must be the declared
            # callable.
            function = getattr(function, name)
        if not callable(function):
            raise ValueError('Declared entrypoint is not callable')
        return function

    def __exit__(self, *exc):
        """Remove the candidate import finder and restore the previous skills modules."""

        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in list(sys.modules):
            if name == 'skills' or name.startswith('skills.'):
                sys.modules.pop(name)
        # Restore the saved modules after the candidate has finished executing.
        sys.modules.update(self.previous)


class FixtureEvidenceMissing(ValueError):
    """A fixture call reached a request with no recorded feedback.

    fixture() raises this when its request cursor passes the supplied receipts.
    Validation records the missing interaction as an unfinished check.

    Example::

        try:
            result = fixture(components, component_id, arguments, receipts)
        except FixtureEvidenceMissing as error:
            diagnostic = str(error)

    diagnostic identifies the request whose feedback is still needed.
    """

    pass


class FixtureExecutionError(ValueError):
    """An exception from the component run during a fixture check.

    details is the exception dictionary produced by the executor. Its type and
    message also form this exception's message, while details retains the traceback
    and original argument record for validation diagnostics.

    Example::

        error = FixtureExecutionError(details)
        observed = error.details

    observed is the execution record that the validator can attach to its check.
    """

    def __init__(self, details):
        """Keep the exception details and format its type and message."""

        self.details = details
        super().__init__(details.get('type', 'Exception') + ': ' + details.get('message', ''))


def fixture(components, component_id, arguments, receipts=()):
    """Call a candidate component using an ordered sequence of request receipts.

    Each emitted request consumes the matching recorded feedback. Return the
    component result and observed interactions after every receipt is consumed.
    """
    import tempfile

    observed = []
    cursor = 0

    def request(value):
        """Match the next recorded request, advance the cursor, and return its feedback."""

        nonlocal cursor
        if cursor >= len(receipts):
            raise FixtureEvidenceMissing('Fixture has no feedback for request ' + repr(value))
        evidence = receipts[cursor]
        cursor += 1
        if value != evidence['request']:
            raise ValueError('Fixture request mismatch: ' + repr(value))
        observed.append({'request': value, 'feedback': evidence['feedback']})
        return evidence['feedback']

    async def request_async(value):
        """Return the next recorded feedback through the async request interface."""

        return request(value)

    with tempfile.TemporaryDirectory() as directory, SkillModules(
        components, directory
    ) as modules:
        interface = components[component_id]['interface']
        function = modules.entry(component_id, interface)
        args, kwargs = bind(
            function, arguments, interface, {'request': request, 'request_async': request_async}
        )
        result = function(*args, **kwargs)
        if inspect.isawaitable(result):
            # result: Awaitable[Any] -> Any, the completed async skill return value.
            result = asyncio.run(result)
    # Every recorded interaction must be consumed by the completed call.
    if cursor != len(receipts):
        raise ValueError('Fixture contains unconsumed request evidence')
    return {'result': result, 'requests': observed}
