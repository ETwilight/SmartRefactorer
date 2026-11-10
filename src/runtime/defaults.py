"""Legacy defaults resolve through providers, rather than importing an environment."""

import os
from .registry import ENVIRONMENTS, resolve


def default_environment(actor=None, **configuration):
    if actor is not None:
        configuration['actor'] = actor
    return resolve(ENVIRONMENTS, os.environ.get('EXPERIMENT_ENVIRONMENT', 'techmc')).connect(
        configuration
    )
