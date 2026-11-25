"""Serve local Wiki and source queries over the knowledge subprocess JSON interface."""

from pathlib import Path as _BootstrapPath
import sys as _BootstrapSys

if not __package__:
    _BootstrapSys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
    __package__ = "environments.techmc"
import base64
import json
from pathlib import Path
import sqlite3
import sys

from environments.techmc.paths import DATA_ROOT as ROOT

from environments.techmc.adapters.knowledge import ZimWiki, SourceIndex


def preflight():
    """Open the downloaded ZIM archive and verify the SQLite source-index schema and indexed
    content.
    """

    from libzim.reader import Archive

    archives = sorted((ROOT / 'knowledge/wiki/zim').glob('*.zim'))
    if not archives:
        raise FileNotFoundError('The shared Wiki archive is missing')
    archive = Archive(str(archives[0]))
    source = ROOT / 'knowledge/source-index.sqlite'
    if not source.is_file():
        raise FileNotFoundError('The shared source index is missing')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(source)')}
        if not {'side', 'path', 'text'} <= columns:
            raise ValueError('The shared source index has an invalid schema')
        if connection.execute('SELECT 1 FROM source LIMIT 1').fetchone() is None:
            raise ValueError('The shared source index has no indexed content')
    return {'backend': 'environment-interpreter', 'wiki': 'ready', 'source': 'ready'}


def dispatch(request):
    """Validate a method/params request and execute its Wiki or source query, encoding binary
    assets as base64.
    """

    if (
        not isinstance(request, dict)
        or set(request) != {'method', 'params'}
        or not isinstance(request['params'], dict)
    ):
        raise ValueError('Invalid knowledge worker request')
    method = request['method']
    params = request['params']
    if method in ('wiki.search', 'wiki.read', 'wiki.provenance'):
        return getattr(ZimWiki(), method.split('.')[1])(**params)
    if method == 'wiki.asset':
        # data: bytes; mime: str. The response represents the binary asset as an ASCII base64
        # string.
        data, mime = ZimWiki().asset(**params)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('Asset exceeds 4 MiB response limit')
        return {'mime_type': mime, 'data_base64': base64.b64encode(data).decode()}
    if method in ('source.search', 'source.read'):
        return getattr(SourceIndex(), method.split('.')[1])(**params)
    raise ValueError('Unknown read-only knowledge method: ' + str(method))


def main():
    """Run preflight or one stdin query and print a JSON result or serialized exception."""

    try:
        result = preflight() if sys.argv[1:] == ['--check'] else dispatch(json.load(sys.stdin))
        reply = {'ok': True, 'result': result}
    except Exception as exc:
        # args: list of JSON values, with unsupported exception arguments replaced by their str
        # representation.
        args = []
        for value in exc.args:
            try:
                json.dumps(value, allow_nan=False)
                args.append(value)
            except (TypeError, ValueError):
                args.append(str(value))
        reply = {
            'ok': False,
            'error': {'type': type(exc).__name__, 'message': str(exc), 'args': args},
        }
    print(json.dumps(reply, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
