"""Small public ports shared by method plugins and environment providers."""

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class EnvironmentPort(Protocol):
    def handle(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def close(self) -> Mapping[str, Any]: ...
    def tool_metadata(self, method: str) -> Mapping[str, Any]: ...
    def readonly(self, request: Mapping[str, Any]) -> bool: ...
    def project_observations(self, observations) -> Mapping[str, Any]: ...
    def knowledge_methods(self): ...
    def knowledge_request(self, method, params, request_id): ...


class MethodPlugin(Protocol):
    def run(self, context: 'RunContext') -> Mapping[str, Any]: ...


class EnvironmentProvider(Protocol):
    def connect(self, configuration: Mapping[str, Any]) -> EnvironmentPort: ...
    def wrap(self, environment) -> EnvironmentPort: ...
    def auditor(self, folder, environment: EnvironmentPort): ...
    def slots(self, ports=None): ...
    def probe(self, slot, heap_mb=2048, controller_mb=512, reserved_mb=0): ...
    def run_allocated(self, configuration, command, cwd): ...


@dataclass(frozen=True)
class RunContext:
    environment: EnvironmentPort
    layout: Any
    goal: str
    total_tokens: int = 50_000_000
    max_rounds: int | None = None
    round_tokens: int | None = None
    options: Mapping[str, Any] | None = None
    # Trusted provider output, never populated from serialized method options.
    recovery_approval: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.total_tokens <= 0:
            raise ValueError('total_tokens must be positive')
        for name in ('max_rounds', 'round_tokens'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(name + ' must be positive or null')


def obj(properties, required=None):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(properties) if required is None else required,
        'additionalProperties': False,
    }


def validate(name, value):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (Path(__file__).parent / 'schemas' / (name + '.schema.json')).read_text(
            encoding='utf-8'
        )
    )
    Draft202012Validator(schema).validate(value)
