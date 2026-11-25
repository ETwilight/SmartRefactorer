"""Expose an existing environment through the configured provider interface."""

import os
from .registry import ENVIRONMENTS, resolve


def as_port(environment):
    """Return an environment port, wrapping the supplied object through its provider when
    needed.
    """

    if hasattr(environment, 'contracts') and hasattr(environment, 'tool_metadata'):
        return environment
    provider = resolve(ENVIRONMENTS, os.environ.get('EXPERIMENT_ENVIRONMENT', 'techmc'))
    return provider.wrap(environment)
