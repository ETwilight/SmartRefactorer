"""Provider wrapping for legacy raw callers; native bridges depend only on this port."""

import os
from .registry import ENVIRONMENTS, resolve


def as_port(environment):
    if hasattr(environment, 'contracts') and hasattr(environment, 'tool_metadata'):
        return environment
    provider = resolve(ENVIRONMENTS, os.environ.get('EXPERIMENT_ENVIRONMENT', 'techmc'))
    return provider.wrap(environment)
