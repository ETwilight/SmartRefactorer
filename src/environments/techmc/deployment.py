"""Portable server inventory shared by deployment and read-only version checks."""

import hashlib
import json
import re
from pathlib import Path

if __package__:
    from .paths import ASSET_ROOT, SOURCE_ROOT
else:
    ASSET_ROOT = SOURCE_ROOT = Path(__file__).resolve().parent


def portable_python(name, source):
    """Emit actual source for the independently deployed flat server layout."""
    text = source.read_text(encoding='utf-8')
    parent = 'parents[1]' if '/' in name else 'parent'
    # 源码使用仓库资产根；部署文件按相同资源相对位置使用安装根。
    # 匹配表达式中的空白，使源码换行和空格与打包路径转换分别处理。
    pattern = (
        r'Path\(__file__\)\.resolve\(\)\.parents\[[23]\]\s*/\s*'
        r'''(?P<quote>['"])assets/techmc(?P<tail>[^'"]*)(?P=quote)'''
    )

    def installed_path(match):
        suffix = match.group('tail').lstrip('/')
        base = f'Path(__file__).resolve().{parent}'
        return base + (' / ' + repr(suffix) if suffix else '')

    text = re.sub(pattern, installed_path, text)
    if name.startswith('tools/'):
        text = text.replace(
            'Path(__file__).resolve().parents[3]', 'Path(__file__).resolve().parents[1]'
        )
        text = text.replace(
            '_ToolPath(__file__).resolve().parents[3]',
            '_ToolPath(__file__).resolve().parents[1]',
        )
    text = text.replace('from environments.techmc.tools.', 'from tools.')
    text = text.replace('from environments.techmc.adapters.', 'from adapters.')
    text = text.replace('from environments.techmc.', 'from ')
    return text.encode('utf-8')


def sha256(source):
    if isinstance(source, bytes):
        return hashlib.sha256(source).hexdigest()
    digest = hashlib.sha256()
    with Path(source).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def assets(root):
    root = Path(root)
    files = {}

    def add(path):
        relative = path.relative_to(root).as_posix()
        if not path.exists() and root.resolve() == ASSET_ROOT.resolve():
            source = SOURCE_ROOT / relative
            if relative.endswith('.py') and source.is_file():
                if source.is_symlink():
                    raise ValueError('Linked deployment source: ' + str(source))
                files[relative] = portable_python(relative, source)
                return
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked deployment asset: ' + str(path))
        if path.is_dir():
            for child in path.iterdir():
                add(child)
        elif path.is_file():
            files[path.relative_to(root).as_posix()] = path
        else:
            raise FileNotFoundError(path)

    for name in (
        'contracts.py',
        'task_catalog.py',
        'task_selection.py',
        'remote_host.py',
        'deployment.py',
        'host_resources.py',
        'host_monitor.py',
        'experiment_registry.py',
        'world_retention.py',
        'task_worlds.py',
        'adapters/bridge.py',
        'tools/void_server.py',
        'tools/bootstrap.py',
        'tasks',
        'schemas',
        'bridge/build/minecraft-agent-26.1.jar',
        'runtime/void-research-server.json',
    ):
        add(root / name)
    template = root / 'runtime/void-research-server'
    for name in (
        'fabric-server-launch.jar',
        'libraries',
        'versions',
        '.fabric/server',
        'eula.txt',
    ):
        add(template / name)
    for name in ('ops.json', 'whitelist.json', 'banned-players.json', 'banned-ips.json'):
        if (template / name).is_file():
            add(template / name)
    if 'eula=true' not in (template / 'eula.txt').read_text().lower():
        raise ValueError('Existing Minecraft EULA acceptance required')
    receipt = json.loads(
        (root / 'runtime/void-research-server.json').read_text(encoding='utf-8')
    )
    datapack = root / 'runtime/datapacks' / receipt['pack']
    add(datapack)
    if sha256(datapack) != receipt['pack_sha256']:
        raise ValueError('Empty-world datapack checksum mismatch')
    lock = json.loads((root / 'runtime/mods.candidate-lock.json').read_text(encoding='utf-8'))
    portable = []
    for mod in lock['mods']:
        if mod['project'] not in ('fabric-api', 'carpet'):
            continue
        source = Path(mod['file'])
        if not source.is_absolute():
            source = root / source
        if sha256(source) != mod['sha256']:
            raise ValueError('Mod checksum mismatch: ' + source.name)
        add(source)
        portable.append({**mod, 'file': source.relative_to(root).as_posix()})
    # Stable bytes on Windows and Linux; do not fingerprint host-native path rewrites.
    files['runtime/mods.candidate-lock.json'] = json.dumps(
        {**lock, 'mods': portable}, indent=2
    ).encode('utf-8')
    build_path = root / 'bridge/build/build.json'
    if build_path.is_file():
        build = json.loads(build_path.read_text(encoding='utf-8'))
        if sha256(root / 'bridge/build/minecraft-agent-26.1.jar') != build['jar_sha256']:
            raise ValueError(
                'Bridge JAR differs from its build receipt; rebuild before deployment'
            )
        for name, expected in build.get('sources', {}).items():
            source = root / Path(name.replace('\\', '/'))
            if not source.is_file() or sha256(source) != expected:
                raise ValueError('Bridge source changed since build: ' + name)
    return files


def manifest(root, inventory=None):
    return {
        name: sha256(source)
        for name, source in (inventory if inventory is not None else assets(root)).items()
    }
