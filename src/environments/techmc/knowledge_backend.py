"""Local knowledge runs in the environment interpreter, independent of a method's venv."""

import builtins
import json
import os
from pathlib import Path
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[2] / "assets/techmc"
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
        reply = json.loads(process.stdout)
    except ValueError as exc:
        raise RuntimeError('Malformed environment knowledge worker response') from exc
    if reply.get('ok') is not True:
        error = reply.get('error', {})
        name = error.get('type', 'RuntimeError')
        exception = getattr(builtins, name, None) or getattr(sqlite3, name, None)
        if not isinstance(exception, type) or not issubclass(exception, Exception):
            exception = RuntimeError
        raise exception(*error.get('args', [error.get('message', 'Knowledge query failed')]))
    return reply['result']


def preflight():
    """Check the shared corpus/dependencies before any model or game allocation."""
    return _run(check=True)


def query(method, params):
    if method not in METHODS:
        raise ValueError('Unknown read-only knowledge method: ' + str(method))
    return _run({'method': method, 'params': params})
