"""Register method and environment factories and import their implementations when selected."""

from importlib import import_module
from pathlib import Path

ENVIRONMENTS = {'techmc': 'adapters.environments.techmc.gateway:TechMCProvider'}
_METHOD_PLUGINS = {
    'smartrefactorer': 'adapters.methods.smartrefactorer.plugin:Plugin',
    'baseline': 'adapters.methods.baseline.plugin:BaselinePlugin',
    'psn': 'adapters.methods.psn.plugin:PSNPlugin',
    'skillzip': 'adapters.methods.skillzip.plugin:SkillZipPlugin',
    'speedrunner': 'adapters.methods.speedrunner.plugin:SpeedRunnerPlugin',
}

# Register the method plugins present in this source tree.
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
METHODS = {
    name: target
    for name, target in _METHOD_PLUGINS.items()
    if _SOURCE_ROOT.joinpath(*target.split(':', 1)[0].split('.')).with_suffix('.py').is_file()
}


def register_environment(name, target):
    """Add a named environment factory to the registry."""

    if name in ENVIRONMENTS:
        raise ValueError('Environment already registered: ' + name)
    ENVIRONMENTS[name] = target


def register_method(name, target):
    """Add a named method factory to the registry."""

    if name in METHODS:
        raise ValueError('Method already registered: ' + name)
    METHODS[name] = target


def resolve(registry, name):
    """Instantiate a registered factory or the factory named by a module:symbol string."""
    # target: str | Callable[[], object]; strings name a module and its zero-argument factory.

    target = registry[name]
    if not isinstance(target, str):
        return target()
    module, symbol = target.split(':', 1)
    return getattr(import_module(module), symbol)()
