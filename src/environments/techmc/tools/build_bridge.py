"""Compile the TechMC bridge against installed Minecraft and Fabric JARs."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import json
from pathlib import Path
import subprocess
import os
import zipfile
from environments.techmc.tools.bootstrap import (
    ROOT,
    MC,
    download,
    extract_safe,
    digest,
    write_json,
)


from environments.techmc.paths import ASSET_ROOT


def build():
    """Compile Java sources, package bridge resources, and record source and JAR hashes.

    The configured JDK supplies javac. Installed libraries form its classpath;
    an argument file carries paths containing spaces into the compiler.
    """

    java = Path(json.loads((ROOT / 'runtime/java.json').read_text())['java'])
    loader = json.loads((ROOT / 'runtime/fabric-loader.json').read_text())['loader']['version']
    loader_jar = download(
        f'https://maven.fabricmc.net/net/fabricmc/fabric-loader/{loader}/fabric-loader-{loader}.jar',
        ROOT / f'runtime/tools/fabric-loader-{loader}.jar',
    )
    mods = json.loads((ROOT / 'runtime/mods.candidate-lock.json').read_text())['mods']
    api = next(m for m in mods if m['project'] == 'fabric-api')
    extract_safe(api['file'], ROOT / 'runtime/fabric-api')
    libraries = list((MC / 'libraries').rglob('*.jar')) + [loader_jar, MC / 'jars/client.jar']
    # Fabric API exposes modules as nested JARs; javac needs those entries explicitly.
    libraries += list((ROOT / 'runtime/fabric-api/META-INF/jars').glob('*.jar'))
    libraries += [Path(m['file']) for m in mods]
    libraries += list(
        (ROOT / 'runtime/void-research-server/libraries/net/fabricmc/sponge-mixin').rglob(
            '*.jar'
        )
    )
    build = ASSET_ROOT / 'bridge/build'
    classes = build / 'classes'
    classes.mkdir(parents=True, exist_ok=True)
    sources = sorted((ASSET_ROOT / 'bridge/src').rglob('*.java'))
    args = [
        '--release',
        '25',
        '-proc:none',
        '-encoding',
        'UTF-8',
        '-cp',
        os.pathsep.join(str(p) for p in libraries),
        '-d',
        str(classes),
    ] + [str(p) for p in sources]
    argfile = build / 'javac.args'
    argfile.write_text(
        '\n'.join('"' + a.replace('\\', '/') + '"' for a in args), encoding='utf-8'
    )
    subprocess.run([str(java.with_name('javac.exe')), '@' + str(argfile)], check=True)
    for resource in (ASSET_ROOT / 'bridge/resources').rglob('*'):
        if resource.is_file():
            target = classes / resource.relative_to(ASSET_ROOT / 'bridge/resources')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resource.read_bytes())
    jar = build / 'minecraft-agent-26.1.jar'
    # Package directly in the workspace. The JDK jar tool uses a temporary path
    # whose final move can cross Windows AppContainer boundaries.
    with zipfile.ZipFile(jar, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(classes).as_posix())
    write_json(
        build / 'build.json',
        {
            'java': str(java),
            'minecraft': '26.1',
            'jar_sha256': digest(jar),
            'sources': {str(p.relative_to(ASSET_ROOT)): digest(p) for p in sources},
            'compiled': True,
        },
    )
    print(jar)


if __name__ == '__main__':
    build()
