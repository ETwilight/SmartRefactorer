"""Reproducible acquisition. Never starts Minecraft or accepts its EULA."""

from __future__ import annotations
from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.parse
import zipfile

ROOT = Path(__file__).resolve().parents[3] / "assets/techmc"
MC = ROOT / 'knowledge/minecraft/26.1'
UA = 'CS269-MinecraftResearch/0.1 (local reproducible acquisition)'


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding='utf-8')
    # Publish the same completed bytes after a transient Windows reader/scanner
    # lock. Never rerun a lifecycle operation or rewrite/serialize on retry.
    for attempt in range(9):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 8:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.25))


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as f:
        for data in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def fetch_json(url):
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={'User-Agent': UA}), timeout=90
    ) as r:
        return json.load(r)


def download(url, path, expected=None, algorithm='sha256', size=None, quiet=False):
    """Atomic verified downloads; resume only against matching strong ETag/Last-Modified."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if expected and digest(path, algorithm) != expected:
            raise ValueError(f'Existing cache hash mismatch: {path}')
        if size is not None and path.stat().st_size != size:
            raise ValueError(f'Existing cache length mismatch: {path}')
        if not expected:
            receipt = path.with_suffix(path.suffix + '.provenance.json')
            if (
                not receipt.exists()
                or json.loads(receipt.read_text())['sha256'] != digest(path)
                or json.loads(receipt.read_text())['url'] != url
            ):
                raise ValueError(f'Unverified existing cache: {path}')
        return path
    part = path.with_suffix(path.suffix + '.part')
    state_path = part.with_suffix(part.suffix + '.json')
    for attempt in range(5):
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            offset = (
                part.stat().st_size
                if part.exists() and state.get('url') == url and state.get('validator')
                else 0
            )
            headers = {'User-Agent': UA, 'Accept-Encoding': 'identity'}
            if offset:
                headers.update({'Range': f'bytes={offset}-', 'If-Range': state['validator']})
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=120
            ) as r:
                if offset and r.status == 206:
                    content_range = r.headers.get('Content-Range', '')
                    if not content_range.startswith(f'bytes {offset}-'):
                        raise ValueError('Invalid Content-Range')
                    total = int(content_range.split('/')[-1])
                    mode = 'ab'
                else:
                    if r.status != 200:
                        raise ValueError(f'Unexpected download status {r.status}')
                    offset, mode = 0, 'wb'
                    total = (
                        int(r.headers['Content-Length'])
                        if r.headers.get('Content-Length')
                        else size
                    )
                validator = r.headers.get('ETag') or r.headers.get('Last-Modified')
                if validator and validator.startswith('W/'):
                    validator = r.headers.get('Last-Modified')
                write_json(state_path, {'url': url, 'validator': validator, 'total': total})
                if not quiet:
                    print(f'Download {path.name}: {offset}/{total}', flush=True)
                with part.open(mode) as out:
                    shutil.copyfileobj(r, out, 1024 * 1024)
            if total is not None and part.stat().st_size != total:
                raise ValueError('Incomplete response')
            if size is not None and part.stat().st_size != size:
                raise ValueError('Official size mismatch')
            if expected and digest(part, algorithm) != expected:
                part.unlink()
                raise ValueError('Official digest mismatch')
            sha256 = digest(part)
            part.replace(path)
            write_json(
                path.with_suffix(path.suffix + '.provenance.json'),
                {
                    'url': url,
                    'acquired_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'size': path.stat().st_size,
                    'sha256': sha256,
                    'publisher_checksum': {algorithm: expected} if expected else None,
                    'verification': (
                        'publisher checksum' if expected else 'transport + local hash only'
                    ),
                },
            )
            state_path.unlink(missing_ok=True)
            return path
        except Exception:
            if attempt == 4:
                raise
            time.sleep(min(2**attempt, 8))


def extract_safe(archive, destination):
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (destination / info.filename).resolve()
            if (
                not target.is_relative_to(destination)
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError('Unsafe archive path')
        z.extractall(destination)


def minecraft():
    manifest = fetch_json('https://piston-meta.mojang.com/mc/game/version_manifest_v2.json')
    entry = next(v for v in manifest['versions'] if v['id'] == '26.1')
    write_json(MC / 'metadata/version_manifest_v2.json', manifest)
    version_file = download(entry['url'], MC / 'metadata/26.1.json', entry['sha1'], 'sha1')
    version = json.loads(version_file.read_text())
    assert version['id'] == '26.1' and version['javaVersion']['majorVersion'] == 25
    if 'client_mappings' in version['downloads']:
        raise ValueError('Unexpected mapping scheme: review metadata')
    old = ROOT / 'knowledge/26.1-version.json'
    write_json(
        MC / 'metadata/handoff_comparison.json',
        {
            'same_as_handoff_metadata': (
                json.loads(old.read_text()) == version if old.exists() else None
            ),
            'entry': entry,
            'naming': 'official unobfuscated',
        },
    )
    tasks = []
    for side in ('client', 'server'):
        d = version['downloads'][side]
        tasks.append((d['url'], MC / f'jars/{side}.jar', d['sha1'], 'sha1', d['size']))
    for lib in version['libraries']:
        d = lib.get('downloads', {}).get('artifact')
        if d and 'native' not in lib['name'].lower():
            tasks.append((d['url'], MC / 'libraries' / d['path'], d['sha1'], 'sha1', d['size']))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda args: download(*args), tasks))
    with zipfile.ZipFile(MC / 'jars/server.jar') as z:
        for listing, folder in [
            ('META-INF/versions.list', 'jars'),
            ('META-INF/libraries.list', 'libraries/server'),
        ]:
            for line in z.read(listing).decode().splitlines():
                sha, name, path = line.split('\t')
                target = (MC / folder / path).resolve()
                if not target.is_relative_to((MC / folder).resolve()):
                    raise ValueError('Unsafe bundled path')
                data = z.read(
                    'META-INF/' + ('versions/' if folder == 'jars' else 'libraries/') + path
                )
                if hashlib.sha256(data).hexdigest() != sha:
                    raise ValueError('Bundled digest mismatch')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if folder == 'jars':
                    write_json(
                        MC / 'metadata/server_inner.json', {'path': str(target), 'sha256': sha}
                    )
    download(
        'https://github.com/Vineflower/vineflower/releases/download/1.12.0/vineflower-1.12.0.jar',
        ROOT / 'runtime/tools/vineflower-1.12.0.jar',
    )


def wiki():
    from environments.techmc.tools.download_zim import main

    main()


def jdk():
    saved = ROOT / 'runtime/java.json'
    releases = (
        [json.loads(saved.read_text())['release']]
        if saved.exists()
        else fetch_json(
            'https://api.adoptium.net/v3/assets/latest/25/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse'
        )
    )
    pkg = releases[0]['binary']['package']
    archive = download(
        pkg['link'], ROOT / 'runtime/downloads' / pkg['name'], pkg['checksum'], size=pkg['size']
    )
    extract_safe(archive, ROOT / 'runtime/java')
    java = next((ROOT / 'runtime/java').glob('*/bin/java.exe'))
    write_json(ROOT / 'runtime/java.json', {'java': str(java), 'release': releases[0]})


def mods():
    entries = []
    lock = ROOT / 'runtime/mods.candidate-lock.json'
    existing = (
        {m['project']: m for m in json.loads(lock.read_text())['mods']} if lock.exists() else {}
    )
    for slug in ('fabric-api', 'malilib', 'litematica', 'minihud', 'tweakeroo', 'carpet'):
        query = urllib.parse.urlencode({'game_versions': '["26.1"]', 'loaders': '["fabric"]'})
        versions = (
            [fetch_json('https://api.modrinth.com/v2/version/' + existing[slug]['version_id'])]
            if slug in existing
            else fetch_json(f'https://api.modrinth.com/v2/project/{slug}/version?{query}')
        )
        versions = [
            v
            for v in versions
            if v['version_type'] == 'release' and '26.1' in v['game_versions']
        ]
        if not versions:
            raise ValueError(f'No exact 26.1 release found for {slug}')
        # Prefer files built explicitly for 26.1; keep multi-version candidates only
        # when no exact build exists, and never label them launch-validated.
        # Select a coherent release generation. Later filenames may say 26.1.2
        # while publisher game_versions and embedded ranges explicitly include 26.1.
        v = versions[0]
        f = next((f for f in v['files'] if f.get('primary')), v['files'][0])
        path = download(
            f['url'],
            ROOT / 'runtime/mods' / f['filename'],
            f['hashes']['sha512'],
            'sha512',
            f['size'],
        )
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read('fabric.mod.json'))
        entries.append(
            {
                'project': slug,
                'version_id': v['id'],
                'version': v['version_number'],
                'game_versions': v['game_versions'],
                'url': f['url'],
                'file': str(path),
                'sha256': digest(path),
                'fabric_mod_json': meta,
            }
        )
        write_json(
            ROOT / 'runtime/mods.candidate-lock.json',
            {
                'minecraft': '26.1',
                'status': 'downloaded; launch validation pending',
                'mods': entries,
            },
        )
    saved = ROOT / 'runtime/fabric-loader.json'
    stable = (
        json.loads(saved.read_text())
        if saved.exists()
        else next(
            v
            for v in fetch_json('https://meta.fabricmc.net/v2/versions/loader/26.1')
            if v['loader']['stable']
        )
    )
    write_json(ROOT / 'runtime/fabric-loader.json', stable)


def decompile(side):
    java_config = ROOT / 'runtime/java.json'
    java = json.loads(java_config.read_text())['java'] if java_config.exists() else 'java'
    jar = (
        MC / 'jars/client.jar'
        if side == 'client'
        else Path(json.loads((MC / 'metadata/server_inner.json').read_text())['path'])
    )
    output = MC / 'sources' / side
    log = MC / 'logs' / (side + '.log')
    output.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    deps = sorted((MC / 'libraries').rglob('*.jar'))
    command = [
        java,
        '-Xmx4G',
        '-jar',
        str(ROOT / 'runtime/tools/vineflower-1.12.0.jar'),
        '--folder',
        '--thread-count=4',
        '--skip-extra-files=1',
    ]
    # Argument file avoids Windows' command-line length limit.
    args = [f'-e={p.as_posix()}' for p in deps] + [jar.as_posix(), output.as_posix()]
    argfile = MC / 'logs' / (side + '.args')
    argfile.write_text(
        '\n'.join('"' + a.replace('\\', '/') + '"' for a in command[1:] + args),
        encoding='utf-8',
    )
    start = time.time()
    with log.open('w', encoding='utf-8') as f:
        result = subprocess.run([java, '@' + str(argfile)], stdout=f, stderr=subprocess.STDOUT)
    files = list(output.rglob('*.java'))
    errors = []
    for p in files:
        for n, line in enumerate(
            p.read_text(encoding='utf-8', errors='replace').splitlines(), 1
        ):
            if '$VF: Couldn\'t' in line or 'could not be decompiled' in line.lower():
                errors.append(
                    {'file': str(p.relative_to(output)), 'line': n, 'text': line.strip()}
                )
    key_classes = {
        name: [str(p.relative_to(output)) for p in files if name in p.name]
        for name in [
            'PistonStructureResolver',
            'PistonBaseBlock',
            'PistonMovingBlockEntity',
            'ObserverBlock',
            'HopperBlockEntity',
        ]
    }
    write_json(
        MC / 'logs' / (side + '.report.json'),
        {
            'side': side,
            'command': command + args,
            'input_sha256': digest(jar),
            'tool_sha256': digest(ROOT / 'runtime/tools/vineflower-1.12.0.jar'),
            'exit_code': result.returncode,
            'elapsed_seconds': time.time() - start,
            'java_files': len(files),
            'failed_methods': errors,
            'key_classes': key_classes,
            'status': (
                'complete'
                if result.returncode == 0 and files and all(key_classes.values()) and not errors
                else 'review required'
            ),
        },
    )
    if result.returncode or not files:
        raise RuntimeError(f'Decompiler failed: {log}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'target',
        choices=['minecraft', 'wiki', 'jdk', 'mods', 'decompile-client', 'decompile-server'],
    )
    ns = parser.parse_args()
    if ns.target.startswith('decompile-'):
        decompile(ns.target.split('-')[1])
    else:
        globals()[ns.target]()
