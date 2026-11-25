"""Build the portable server inventory and its content hashes."""

import hashlib
import json
import re
from pathlib import Path

if __package__:
    from .paths import ASSET_ROOT, SOURCE_ROOT, DATA_ROOT
else:
    ASSET_ROOT = SOURCE_ROOT = DATA_ROOT = Path(__file__).resolve().parent


def portable_python(name, source):
    """Render a Python source file for the installed server layout as UTF-8 bytes.

    Rewrite source-asset paths and package imports to their installed locations.
    paths.py is generated for the flat server root.
    """
    if name == 'paths.py':
        return (
            'from pathlib import Path\n'
            'SOURCE_ROOT = ASSET_ROOT = DATA_ROOT = Path(__file__).resolve().parent\n'
            'RESEARCH_ROOT = SOURCE_ROOT\n'
        ).encode('utf-8')
    text = source.read_text(encoding='utf-8')
    parent = 'parents[1]' if '/' in name else 'parent'
    # Source assets and installed assets occupy corresponding relative locations.
    # Allow source whitespace to vary when replacing the asset-root expression.
    pattern = (
        r'Path\(__file__\)\.resolve\(\)\.parents\[[23]\]\s*/\s*'
        r'''(?P<quote>['"])assets/techmc(?P<tail>[^'"]*)(?P=quote)'''
    )

    def installed_path(match):
        """Replace a matched source-asset path with its installed relative expression."""

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
    """Hash byte content directly, or stream a path's file content into SHA256."""

    if isinstance(source, bytes):
        return hashlib.sha256(source).hexdigest()
    digest = hashlib.sha256()
    with Path(source).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def assets(root):
    """Collect portable sources, server dependencies, tasks, schemas, and build assets.

    Return a mapping from portable relative names to Paths or rendered bytes.
    Check the datapack, selected mod hashes, and bridge build receipt while
    collecting the inventory.
    """

    root = Path(root)
    # Inventory values are Path objects for files or bytes for rendered content.
    files = {}

    def add(path, relative=None):
        """Collect a file or directory under its portable name, rendering owned Python
        sources.
        """

        relative = relative or path.relative_to(root).as_posix()
        if root.resolve() == DATA_ROOT.resolve():
            source = (
                SOURCE_ROOT / relative if relative.endswith('.py') else ASSET_ROOT / relative
            )
            if relative.endswith('.py') and source.is_file():
                if source.is_symlink():
                    raise ValueError('Linked deployment source: ' + str(source))
                # Store the deployed Python bytes so version checks hash the rendered layout.
                files[relative] = portable_python(relative, source)
                return
            if not path.exists() and source.exists():
                path = source
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked deployment asset: ' + str(path))
        if path.is_dir():
            for child in path.iterdir():
                add(child, relative + '/' + child.name)
        elif path.is_file():
            files[relative] = path
        else:
            raise FileNotFoundError(path)

    for name in (
        'contracts.py',
        'paths.py',
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
    # Use portable relative mod paths to produce identical bytes across hosts.
    files['runtime/mods.candidate-lock.json'] = json.dumps(
        {**lock, 'mods': portable}, indent=2
    ).encode('utf-8')
    build_root = ASSET_ROOT if root.resolve() == DATA_ROOT.resolve() else root
    build_path = build_root / 'bridge/build/build.json'
    if build_path.is_file():
        build = json.loads(build_path.read_text(encoding='utf-8'))
        if sha256(build_root / 'bridge/build/minecraft-agent-26.1.jar') != build['jar_sha256']:
            raise ValueError(
                'Bridge JAR differs from its build receipt; rebuild before deployment'
            )
        for name, expected in build.get('sources', {}).items():
            source = build_root / Path(name.replace('\\', '/'))
            if not source.is_file() or sha256(source) != expected:
                raise ValueError('Bridge source changed since build: ' + name)
    return files


def manifest(root, inventory=None):
    """Map each portable inventory name to the hash of its deployed content."""

    return {
        name: sha256(source)
        for name, source in (inventory if inventory is not None else assets(root)).items()
    }
