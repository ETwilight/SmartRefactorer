"""Offline, backed-up block/entity reset of ONLY the dedicated research world.

Keep its documented 5x5 stone landing platform and all biome/generator metadata.
Default is dry-run. Requires confirmed server shutdown and validates all output.
"""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import socket
import time
import zipfile
import zlib
import nbtlib as nbt

ROOT = Path(__file__).resolve().parents[3] / "assets/techmc"
MASK = (1 << 64) - 1


def records(path):
    with path.open('rb') as stream:
        header = stream.read(8192)
        if not header:
            return bytes(8192), []  # Vanilla creates empty region placeholders.
        if len(header) != 8192:
            raise ValueError('Invalid region header: ' + str(path))
        result = []
        for index in range(1024):
            offset = int.from_bytes(header[index * 4 : index * 4 + 3], 'big')
            if not offset:
                continue
            stream.seek(offset * 4096)
            length = int.from_bytes(stream.read(4), 'big')
            kind = stream.read(1)[0]
            data = stream.read(length - 1)
            if kind == 1:
                raw = gzip.decompress(data)
            elif kind == 2:
                raw = zlib.decompress(data)
            elif kind == 3:
                raw = data
            else:
                raise ValueError('Unsupported/external chunk encoding: ' + str(kind))
            result.append((index, nbt.File.parse(io.BytesIO(raw))))
        return header, result


def block_counts(chunk):
    counts = Counter()
    for section in chunk.get('sections', []):
        state = section.get('block_states', {})
        palette = state.get('palette', [])
        if len(palette) == 1:
            counts[str(palette[0]['Name'])] += 4096
        elif palette:
            bits = max(4, (len(palette) - 1).bit_length())
            per_long = 64 // bits
            data = state['data']
            mask = (1 << bits) - 1
            for cell in range(4096):
                value = (int(data[cell // per_long]) & MASK) >> ((cell % per_long) * bits)
                counts[str(palette[value & mask]['Name'])] += 1
    return counts


def reset_chunk(chunk, dimension, spawn):
    x, y, z = spawn
    platform_y = y - 1
    platform_chunk = (x // 16, z // 16)
    preserve = (
        dimension == 'minecraft:overworld'
        and (int(chunk['xPos']), int(chunk['zPos'])) == platform_chunk
    )
    for section in chunk.get('sections', []):
        palette = [nbt.Compound({'Name': nbt.String('minecraft:air')})]
        state = nbt.Compound({'palette': nbt.List[nbt.Compound](palette)})
        if preserve and int(section['Y']) == platform_y // 16:
            palette.append(nbt.Compound({'Name': nbt.String('minecraft:stone')}))
            state['palette'] = nbt.List[nbt.Compound](palette)
            values = [0] * 256
            for px in range(x - 2, x + 3):
                for pz in range(z - 2, z + 3):
                    if (px // 16, pz // 16) != platform_chunk:
                        raise ValueError('Platform crosses chunks; unsupported reset layout')
                    cell = ((platform_y % 16) << 8) | ((pz % 16) << 4) | (px % 16)
                    values[cell // 16] |= 1 << ((cell % 16) * 4)
            state['data'] = nbt.LongArray(values)
        section['block_states'] = state
        for key in ('BlockLight', 'SkyLight'):
            section.pop(key, None)
    chunk['isLightOn'] = nbt.Byte(0)
    chunk['Heightmaps'] = nbt.Compound()
    for key in ('block_entities', 'entities', 'block_ticks', 'fluid_ticks'):
        chunk[key] = nbt.List[nbt.Compound]()
    chunk.pop('PostProcessing', None)


def write_region(path, header, chunks):
    location = bytearray(4096)
    timestamps = bytearray(header[4096:8192])
    bodies = []
    sector = 2
    for index, chunk in chunks:
        buffer = io.BytesIO()
        chunk.write(buffer)
        compressed = zlib.compress(buffer.getvalue(), level=1)
        body = (len(compressed) + 1).to_bytes(4, 'big') + b'\x02' + compressed
        count = (len(body) + 4095) // 4096
        if count > 255:
            raise ValueError('Chunk exceeds inline region sector limit')
        location[index * 4 : index * 4 + 4] = sector.to_bytes(3, 'big') + bytes([count])
        timestamps[index * 4 : index * 4 + 4] = int(time.time()).to_bytes(4, 'big')
        bodies.append(body.ljust(count * 4096, b'\0'))
        sector += count
    with path.open('wb') as stream:
        stream.write(location)
        stream.write(timestamps)
        for body in bodies:
            stream.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    server = (ROOT / 'runtime/void-research-server').resolve()
    world = (server / 'agent-void-research').resolve()
    if not world.is_relative_to(ROOT.resolve()) or world.name != 'agent-void-research':
        raise ValueError('Unexpected world path')
    receipt = json.loads(
        (ROOT / 'runtime/void-research-server.json').read_text(encoding='utf-8')
    )
    if Path(receipt['world']).resolve() != world:
        raise ValueError('World does not match environment receipt')
    state = json.loads((server / 'process.json').read_text(encoding='utf-8'))
    if state.get('status') != 'stopped':
        raise RuntimeError('Research server must be fully stopped')
    try:
        connection = socket.create_connection(('127.0.0.1', 25566), timeout=1)
    except OSError:
        pass
    else:
        connection.close()
        raise RuntimeError('Server port is still listening; refusing offline edits')
    spawn = [int(v) for v in nbt.load(world / 'level.dat')['Data']['spawn']['pos']]
    if spawn != [-40, 64, -24]:
        raise ValueError('Initial platform coordinates changed; refusing to guess')
    paths = sorted((world / 'dimensions').rglob('*.mca'))
    if list(world.rglob('*.mcc')):
        raise ValueError('External chunks need a separate reviewed reset')
    files = list(world.rglob('*'))
    if any(p.is_symlink() or not p.resolve().is_relative_to(world) for p in files):
        raise ValueError('World contains external links')
    before = Counter()
    after = Counter()
    plans = []
    chunk_count = 0
    entities = 0
    for path in paths:
        if path.parent.name not in ('region', 'entities', 'poi'):
            raise ValueError('Unknown region kind')
        relative = path.relative_to(world)
        parts = relative.parts
        dimension = parts[1] + ':' + ('/'.join(parts[2:-2]))
        header, chunks = records(path)
        for _, chunk in chunks:
            if path.parent.name == 'region':
                chunk_count += 1
                before.update(block_counts(chunk))
                entities += len(chunk.get('entities', []))
                reset_chunk(chunk, dimension, spawn)
                after.update(block_counts(chunk))
            elif path.parent.name == 'entities':
                entities += len(chunk.get('Entities', []))
                chunk['Entities'] = nbt.List[nbt.Compound]()
            else:
                chunk['Sections'] = nbt.Compound()
        plans.append((path, header, chunks))
    non_air = {
        name: count for name, count in after.items() if name != 'minecraft:air' and count
    }
    if non_air != {'minecraft:stone': 25}:
        raise ValueError('Reset does not preserve exactly the platform: ' + str(non_air))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = {
        'world': str(world),
        'spawn': spawn,
        'region_files': len(paths),
        'chunks': chunk_count,
        'before_non_air': {k: v for k, v in before.items() if k != 'minecraft:air' and v},
        'after_non_air': non_air,
        'removed_entities': entities,
        'preserved': '25 stone blocks, biome palettes, seed, structures metadata, generation settings, datapacks and player data',
        'status': 'dry_run',
    }
    if args.apply:
        backup = ROOT / 'runtime/world-backups' / ('before-clear-' + stamp + '.zip')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            backup, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1
        ) as archive:
            for path in files:
                if path.is_file():
                    archive.write(path, path.relative_to(world).as_posix())
        with zipfile.ZipFile(backup) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('Backup integrity check failed')
        staged = []
        for path, header, chunks in plans:
            temporary = path.with_suffix('.reset.tmp')
            write_region(temporary, header, chunks)
            staged.append((path, temporary))
        check = Counter()
        for original, temporary in staged:
            if original.parent.name == 'region':
                for _, chunk in records(temporary)[1]:
                    check.update(block_counts(chunk))
        if {k: v for k, v in check.items() if k != 'minecraft:air' and v} != {
            'minecraft:stone': 25
        }:
            raise RuntimeError('Staged world verification failed; originals were not replaced')
        for original, temporary in staged:
            temporary.replace(original)
        report.update(
            status='cleared',
            backup=str(backup),
            verification='All staged regions re-read; exactly 25 stone blocks remain',
        )
        output = ROOT / 'runtime/world-backups' / ('clear-report-' + stamp + '.json')
        output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        report['report_file'] = str(output)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
