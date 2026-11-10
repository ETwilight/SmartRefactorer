"""Install the built bridge by ordinary file copies, preserving previous JARs."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
import zipfile

ROOT = Path(__file__).resolve().parents[3] / "assets/techmc"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--client', action='store_true')
    args = parser.parse_args()
    source = ROOT / 'bridge/build/minecraft-agent-26.1.jar'
    with zipfile.ZipFile(source) as archive:
        if json.loads(archive.read('fabric.mod.json'))['id'] != 'techmc_env':
            raise ValueError('Unexpected bridge mod ID')
    targets = [ROOT / 'runtime/void-research-server/mods/minecraft-agent-26.1.jar']
    receipt_path = ROOT / 'runtime/hmcl-installation.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if args.client:
        instance = Path(receipt['instance']).resolve()
        target = (instance / 'mods/minecraft-agent-26.1.jar').resolve()
        if not target.is_relative_to(instance):
            raise ValueError('Client target escaped configured instance')
        targets.append(target)
    installed = []
    for target in targets:
        if target.is_symlink() or (
            hasattr(target.parent, 'is_junction') and target.parent.is_junction()
        ):
            raise ValueError('Linked deployment target refused')
        if target.exists():
            backup = ROOT / 'runtime/bridge-history' / (sha(target) + '.jar')
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copyfile(target, backup)
        shutil.copyfile(source, target)
        if sha(source) != sha(target):
            raise RuntimeError('Bridge copy verification failed')
        installed.append(str(target))
    if args.client:
        receipt['bridge_sha256'] = sha(source)
        receipt['bridge_root'] = str(ROOT)
        receipt['bridge_deployed_ns'] = time.time_ns()
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(
        json.dumps(
            {'mod_id': 'techmc_env', 'sha256': sha(source), 'installed': installed}, indent=2
        )
    )


if __name__ == '__main__':
    main()
