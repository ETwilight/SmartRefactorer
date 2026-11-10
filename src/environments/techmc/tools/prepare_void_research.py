"""Prepare the 26.1 normal-biome void environment using verified assets."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
from environments.techmc.tools.bootstrap import ROOT, digest, download, write_json, fetch_json
import argparse
from pathlib import Path
import json
import shutil
import zipfile

PACK = 'skyvoid_worldgen_empty_v2_0_11-MC_26_1.zip'
PACK_SHA = '1162323f7a90ebfdf83985a98c1efb675db5546b2fbd9cea337b5bd7846d6a01'


def prepare(accept_eula=False):
    target = ROOT / 'runtime/void-research-server'
    receipt_path = ROOT / 'runtime/void-research-server.json'
    if target.exists() and not receipt_path.exists():
        raise ValueError('Existing unowned target preserved')
    if receipt_path.exists():
        print(receipt_path)
        return
    source = download(
        'https://github.com/BPR02/SkyBlock_Collection/releases/download/v3.2.9/' + PACK,
        ROOT / 'runtime/datapacks' / PACK,
        PACK_SHA,
    )
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        metadata = json.loads(archive.read('pack.mcmeta'))
        functions = [name for name in names if name.endswith('.mcfunction')]
        reviewed_functions = {
            'data/load/function/_private/init.mcfunction',
            'data/skyvoid_worldgen/function/v2.0/load.mcfunction',
            'data/skyvoid_worldgen/function/v2.0/load_wait.mcfunction',
            'data/skyvoid_worldgen/function/v2.0/versioning/enumerate.mcfunction',
            'data/skyvoid_worldgen/function/v2.0/versioning/resolve_load.mcfunction',
        }
        # These exact hash-pinned author functions were reviewed: scoreboard
        # version bookkeeping and startup scheduling, no terrain/item changes.
        if set(functions) != reviewed_functions:
            raise ValueError('Unreviewed pack function set')
    target.mkdir(parents=True)
    loader = json.loads((ROOT / 'runtime/fabric-loader.json').read_text())['loader']['version']
    deployment = ROOT / 'runtime/deployment.json'
    installer = (
        json.loads(deployment.read_text())['fabric_installer']
        if deployment.exists()
        else next(
            x['version']
            for x in fetch_json('https://meta.fabricmc.net/v2/versions/installer')
            if x['stable']
        )
    )
    download(
        f'https://meta.fabricmc.net/v2/versions/loader/26.1/{loader}/{installer}/server/jar',
        target / 'fabric-server-launch.jar',
    )
    (target / 'mods').mkdir()
    lock = json.loads((ROOT / 'runtime/mods.candidate-lock.json').read_text())
    for mod in lock['mods']:
        if mod['project'] in ('fabric-api', 'carpet'):
            src = Path(mod['file'])
            if digest(src) != mod['sha256']:
                raise ValueError('Mod digest mismatch')
            shutil.copyfile(src, target / 'mods' / src.name)
    bridge = ROOT / 'bridge/build/minecraft-agent-26.1.jar'
    shutil.copyfile(bridge, target / 'mods' / bridge.name)
    (target / 'eula.txt').write_text(
        'eula=true\n' if accept_eula else 'eula=false\n', encoding='utf-8'
    )
    world = target / 'agent-void-research'
    (world / 'datapacks').mkdir(parents=True)
    shutil.copyfile(source, world / 'datapacks' / PACK)
    (world / 'carpet.conf').write_text('hopperCounters true\n', encoding='utf-8')
    seed = '-7160105646644042722'
    properties = {
        'server-ip': '127.0.0.1',
        'server-port': '25566',
        'online-mode': 'true',
        'enable-rcon': 'false',
        'enable-command-block': 'false',
        'level-name': 'agent-void-research',
        'level-type': 'minecraft:normal',
        'level-seed': seed,
        'generate-structures': 'true',
        'gamemode': 'creative',
        'difficulty': 'normal',
        'allow-flight': 'true',
        'spawn-protection': '0',
        'view-distance': '6',
        'simulation-distance': '6',
        'max-players': '4',
        'pause-when-empty-seconds': '-1',
        'motd': 'Minecraft 26.1 Void Research (normal biomes + structure bounds)',
        'initial-enabled-packs': 'vanilla,fabric-convention-tags-v2,file/' + PACK,
    }
    (target / 'server.properties').write_text(
        ''.join(k + '=' + v + '\n' for k, v in properties.items()), encoding='utf-8'
    )
    receipt = {
        'directory': str(target),
        'world': str(world),
        'minecraft': '26.1',
        'generator': 'minecraft:normal + generation-only empty datapack',
        'seed': seed,
        'game_port': 25566,
        'bridge_port': 8776,
        'pack': PACK,
        'pack_sha256': digest(source),
        'pack_metadata': metadata,
        'pack_functions': functions,
        'pack_file_count': len(names),
        'function_review': 'fixed author startup/version scoreboards and scheduling; no Agent permission to invoke arbitrary functions',
        'bridge_sha256': digest(bridge),
        'existing_world_modified': False,
        'status': 'prepared; real world verification pending',
    }
    write_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--accept-eula', action='store_true')
    prepare(parser.parse_args().accept_eula)
