"""Loopback-only, fully offline ZIM browser/search; no Wiki API requests."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
import html
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit, parse_qs, unquote, quote

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from environments.techmc.adapters.knowledge import ZimWiki


def main(port=8877):
    wiki = ZimWiki()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            try:
                if url.path == '/':
                    text = (
                        '<h1>Minecraft Wiki · offline</h1><form action="/search"><input name="q"><button>Search</button></form><p>Archive: '
                        + html.escape(wiki.path.name)
                        + '</p><a href="/wiki/Piston">Piston</a>'
                    )
                    body = text.encode()
                    mime = 'text/html; charset=utf-8'
                elif url.path == '/search':
                    query = parse_qs(url.query).get('q', [''])[0]
                    with lock:
                        results = wiki.search(query, limit=20)
                    body = (
                        '<h1>Offline results</h1>'
                        + ''.join(
                            '<p><a href="/wiki/'
                            + quote(r['entry'], safe='/')
                            + '">'
                            + html.escape(r['title'])
                            + '</a><br>'
                            + html.escape(r['summary'])
                            + '</p>'
                            for r in results
                        )
                    ).encode()
                    mime = 'text/html; charset=utf-8'
                elif url.path.startswith('/wiki/'):
                    entry = unquote(url.path[6:])
                    body, mime = wiki.asset(entry)
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header(
                    'Content-Security-Policy',
                    "default-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'none'; object-src 'none'",
                )
                self.end_headers()
                self.wfile.write(body)
            except PermissionError:
                self.send_error(
                    403, 'Tutorial pages are excluded by information-only Wiki policy'
                )
            except (KeyError, RuntimeError):
                self.send_error(404, 'Entry not in offline archive')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Offline Wiki: http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
