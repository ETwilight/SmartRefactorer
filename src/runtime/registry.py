"""Lazy registries: importing a CLI never loads all native algorithm stacks."""

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

# 按实际安装的 adapter 注册方法；完整工作区与精简发布包共用这一入口。
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
METHODS = {
    name: target
    for name, target in _METHOD_PLUGINS.items()
    if _SOURCE_ROOT.joinpath(*target.split(':', 1)[0].split('.')).with_suffix('.py').is_file()
}


def register_environment(name, target):
    if name in ENVIRONMENTS:
        raise ValueError('Environment already registered: ' + name)
    ENVIRONMENTS[name] = target


def register_method(name, target):
    if name in METHODS:
        raise ValueError('Method already registered: ' + name)
    METHODS[name] = target


def resolve(registry, name):
    target = registry[name]
    if not isinstance(target, str):
        return target()
    module, symbol = target.split(':', 1)
    return getattr(import_module(module), symbol)()
