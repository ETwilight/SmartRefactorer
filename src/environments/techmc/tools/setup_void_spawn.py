"""Create a small documented landing platform in the otherwise empty world."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import json
from pathlib import Path
import sys
import time
import uuid
import nbtlib

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.techmc.adapters.bridge import Bridge, BridgeError
from environments.techmc.tools.bootstrap import ROOT, write_json


def main():
    world = ROOT / 'runtime/void-research-server/agent-void-research'
    spawn = nbtlib.load(world / 'level.dat').unpack()['Data']['spawn']
    x, y, z = map(int, spawn['pos'])
    bridge = Bridge(
        'void-spawn-' + str(uuid.uuid4()), 'ResearchAgent', 'server', port=8776, timeout=90
    )

    def call(method, params=None):
        request_id = str(uuid.uuid4())
        deadline = time.monotonic() + 120
        while True:
            try:
                return bridge.call(method, params, request_id)['result']
            except BridgeError as exc:
                if (
                    json.loads(str(exc)).get('status') != 'pending_or_unknown'
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.5)
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    session = call('session.describe')
    call('command.run', {'text': f'forceload add {x-2} {z-2} {x+2} {z+2}'})
    deadline = time.monotonic() + 40
    while not call('observe.biome', {'x': x, 'y': y, 'z': z})['loaded']:
        if time.monotonic() >= deadline:
            raise TimeoutError('Spawn chunk not loaded')
        time.sleep(0.2)
    result = call(
        'command.run', {'text': f'fill {x-2} {y-1} {z-2} {x+2} {y-1} {z+2} minecraft:stone'}
    )
    assert any(c['success'] for c in result['callbacks']), result
    radius = call(
        'command.run', {'text': 'gamerule minecraft:respawn_radius 0', 'keep_conditions': True}
    )
    assert any(c['success'] for c in radius['callbacks']), radius
    platform = call(
        'observe.blocks', {'min': [x - 2, y - 1, z - 2], 'max': [x + 2, y - 1, z + 2]}
    )
    assert len(platform['blocks']) == 25 and all(
        b['state'] == 'Block{minecraft:stone}' for b in platform['blocks']
    )
    restored = call('experiment.restore', {'keep': []})
    assert not restored.get('errors'), restored
    clock1 = call('clock.query')
    time.sleep(0.3)
    clock2 = call('clock.query')
    assert clock2['tick'] > clock1['tick'], 'Empty server must continue natural ticks'
    report = {
        'spawn': [x, y, z],
        'platform_min': [x - 2, y - 1, z - 2],
        'platform_max': [x + 2, y - 1, z + 2],
        'stone_blocks': 25,
        'respawn_radius': 0,
        'natural_ticks_without_players': clock2['tick'] - clock1['tick'],
        'cleanup': restored,
        'game_port': 25566,
        'bridge_port': 8776,
        'status': 'passed',
    }
    write_json(ROOT / 'runtime/void-spawn-setup.json', report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
