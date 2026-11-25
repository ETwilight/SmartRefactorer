"""Interfaces and run settings shared by method plugins and environment providers."""

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class EnvironmentPort(Protocol):
    """Public environment operations used by methods and the task runner.

    handle() accepts a request mapping and returns its feedback mapping. close()
    ends the runner's environment session. Tool metadata supplies effect and schema
    information; readonly() classifies a request using that information.
    project_observations() prepares observations for the method, while the knowledge
    methods build the public requests used during planning and development.

    Example, using an environment created by its provider::

        environment = provider.connect(configuration)
        feedback = environment.handle(request)

    feedback contains the public result and execution metadata for this request.
    """

    def handle(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Execute a public request and return its feedback mapping."""
        ...
    def close(self) -> Mapping[str, Any]:
        """Close the environment session and return its cleanup result."""
        ...
    def tool_metadata(self, method: str) -> Mapping[str, Any]:
        """Return the published metadata for the named method."""
        ...
    def readonly(self, request: Mapping[str, Any]) -> bool:
        """Return whether the environment classifies this request as read-only."""
        ...
    def project_observations(self, observations) -> Mapping[str, Any]:
        """Convert native observations into the method-facing observation mapping."""
        ...
    def knowledge_methods(self):
        """Return the public knowledge-method names offered by this environment."""
        ...
    def knowledge_request(self, method, params, request_id):
        """Build a public knowledge request with the supplied parameters and ID."""
        ...


class MethodPlugin(Protocol):
    """Run a method from the environment and settings assembled by the worker.

    The registry resolves a plugin factory. run(context) receives a RunContext and
    returns the method's report mapping after its task runner finishes.

    Example, from worker dispatch::

        plugin = resolve(METHODS, method_name)
        report = plugin.run(context)

    The selected plugin supplies its algorithm adapter to the run.
    """

    def run(self, context: 'RunContext') -> Mapping[str, Any]:
        """Run the method with this context and return its report."""
        ...


class EnvironmentProvider(Protocol):
    """Construct environment ports and allocate resources for worker commands.

    connect() creates an environment from its configuration; wrap() exposes an
    existing environment through the same port. auditor() creates its progress
    observer. slots() lists candidate server slots and probe() reports capacity.
    run_allocated() owns the environment while the worker command is running.

    Example, after resolving the provider in the worker::

        provider = resolve(ENVIRONMENTS, environment_name)
        environment = provider.connect(configuration)

    The method can now use environment.handle() through EnvironmentPort.
    """

    def connect(self, configuration: Mapping[str, Any]) -> EnvironmentPort:
        """Create the configured environment port."""
        ...
    def wrap(self, environment) -> EnvironmentPort:
        """Expose an existing environment through its provider port."""
        ...
    def auditor(self, folder, environment: EnvironmentPort):
        """Create the progress observer for this environment and output folder."""
        ...
    def slots(self, ports=None):
        """List server slots for the supplied ports or the provider defaults."""
        ...
    def probe(self, slot, heap_mb=2048, controller_mb=512, reserved_mb=0):
        """Report slot availability with the requested memory and current reservations."""
        ...
    def run_allocated(self, configuration, command, cwd):
        """Run a worker command while owning its allocated environment."""
        ...


@dataclass(frozen=True)
class RunContext:
    """Settings and connected services passed from a worker to a method plugin.

    environment is the public port and layout identifies the output directories.
    goal is the run objective. total_tokens is the cumulative budget; max_rounds
    and round_tokens optionally limit task attempts and each round's token use.
    options contains method settings and callbacks assembled by the worker.
    recovery_approval carries provider-supplied continuation metadata.

    The dataclass fixes these field assignments after construction. __post_init__
    checks the supplied limits before the plugin starts.

    Example, after connecting the environment and preparing its layout::

        context = RunContext(environment, layout, goal, total_tokens=token_budget)
        report = plugin.run(context)

    The plugin receives the same cumulative budget and environment chosen by the worker.
    """

    environment: EnvironmentPort
    layout: Any
    goal: str
    total_tokens: int = 50_000_000
    max_rounds: int | None = None
    round_tokens: int | None = None
    options: Mapping[str, Any] | None = None
    # Continuation metadata supplied by the environment provider.
    recovery_approval: Mapping[str, Any] | None = None

    def __post_init__(self):
        """Check that the run budget and supplied round limits are positive."""

        if self.total_tokens <= 0:
            raise ValueError('total_tokens must be positive')
        for name in ('max_rounds', 'round_tokens'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(name + ' must be positive or null')


def obj(properties, required=None):
    """Build an object schema with the selected required fields and closed property names."""

    return {
        'type': 'object',
        'properties': properties,
        'required': list(properties) if required is None else required,
        'additionalProperties': False,
    }


def validate(name, value):
    """Load the named runtime schema and validate a value against it."""

    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parent / 'schemas' / (name + '.schema.json')).read_text(
            encoding='utf-8'
        )
    )
    Draft202012Validator(schema).validate(value)
