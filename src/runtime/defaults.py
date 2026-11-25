"""Construct the selected default environment through its registered provider."""

import os
from .registry import ENVIRONMENTS, resolve


def default_environment(actor=None, **configuration):
    """Connect the provider named by EXPERIMENT_ENVIRONMENT, passing the actor and
    configuration.
    """

    if actor is not None:
        configuration['actor'] = actor
    return resolve(ENVIRONMENTS, os.environ.get('EXPERIMENT_ENVIRONMENT', 'techmc')).connect(
        configuration
    )
