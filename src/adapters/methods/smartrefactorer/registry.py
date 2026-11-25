"""Method and environment implementations selected by the SR command-line entrypoint."""

from runtime.registry import resolve

METHODS = {'smartrefactorer': 'adapters.methods.smartrefactorer.plugin:Plugin'}
ENVIRONMENTS = {'techmc': 'adapters.methods.smartrefactorer.provider:Provider'}
