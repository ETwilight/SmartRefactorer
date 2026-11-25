"""Execute Wiki and source queries in the project's TechMC dependency environment."""

import builtins
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from environments.techmc.paths import DATA_ROOT as ROOT

METHODS = frozenset(
    (
        'wiki.search',
        'wiki.read',
        'wiki.provenance',
        'wiki.asset',
        'source.search',
        'source.read',
    )
)


def interpreter():
    """Return the installed project-local TechMC Python interpreter."""

    executable = (
        Path(__file__).resolve().parents[2]
        / '.venvs/techmc'
        / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    )
    if not executable.is_file():
        raise RuntimeError(
            'The environment knowledge interpreter is missing: ' + str(executable)
        )
    return executable


def _run(payload=None, *, check=False):
    """Run the knowledge worker, decode its result, and reconstruct a returned builtin or
    SQLite exception when it fails.
    """

    command = [
        str(interpreter()),
        '-B',
        '-X',
        'utf8',
        str(Path(__file__).with_name('knowledge_worker.py')),
    ]
    if check:
        command.append('--check')
    process = subprocess.run(
        command,
        input=None if check else json.dumps(payload, ensure_ascii=True, allow_nan=False),
        text=True,
        encoding='utf-8',
        capture_output=True,
        timeout=120,
        cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    if process.returncode:
        raise RuntimeError('Environment knowledge worker failed: ' + process.stderr[-2000:])
    try:
        # process.stdout is str; reply is the decoded worker result/error envelope.
        reply = json.loads(process.stdout)
    except ValueError as exc:
        raise RuntimeError('Malformed environment knowledge worker response') from exc
    if reply.get('ok') is not True:
        error = reply.get('error', {})
        name = error.get('type', 'RuntimeError')
        # exception: object | None until the following check selects an Exception subclass.
        exception = getattr(builtins, name, None) or getattr(sqlite3, name, None)
        if not isinstance(exception, type) or not issubclass(exception, Exception):
            exception = RuntimeError
        raise exception(*error.get('args', [error.get('message', 'Knowledge query failed')]))
    return reply['result']


def preflight():
    """Run the knowledge worker's dependency and corpus checks."""
    return _run(check=True)


def query(method, params):
    """Execute one supported knowledge method through the environment interpreter."""

    if method not in METHODS:
        raise ValueError('Unknown read-only knowledge method: ' + str(method))
    return _run({'method': method, 'params': params})
