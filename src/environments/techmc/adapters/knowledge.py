from __future__ import annotations
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import sys
import unicodedata
from urllib.parse import unquote, urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[3] / "assets/techmc"
from environments.techmc.tools.bootstrap import digest


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.assets, self.links, self.headings = [], [], [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag == 'img':
            self.assets.append(a)
        if tag == 'a' and 'href' in a:
            self.links.append(a['href'])
        if tag in ('p', 'div', 'tr', 'h1', 'h2', 'h3', 'li', 'br'):
            self.text.append('\n')
        if tag in ('td', 'th'):
            self.text.append('\t')
        if tag in ('h1', 'h2', 'h3', 'h4') and a.get('id'):
            self.headings.append(a['id'])

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


class ZimWiki:
    @staticmethod
    def information_only(path, title=''):
        for value in (path, title):
            value = unicodedata.normalize('NFKC', str(value))
            for _ in range(3):
                value = unquote(value)
            if '://' in value:
                url = urlsplit(value)
                value = parse_qs(url.query).get('title', [url.path])[0]
            value = (
                value.lstrip('./')
                .removeprefix('A/')
                .removeprefix('w/')
                .replace('_', ' ')
                .casefold()
            )
            if re.match(r'^(?:tutorials?|教程)(?:\s*[:/]|\s*$)', value):
                raise PermissionError(
                    'Wiki policy: tutorial pages are excluded; use mechanism/information articles'
                )

    def _entry(self, path):
        self.information_only(path)
        entry = self.archive.get_entry_by_path(path)
        for _ in range(12):
            self.information_only(entry.path, entry.title)
            if not entry.is_redirect:
                return entry
            entry = entry.get_redirect_entry()
        raise ValueError('Wiki redirect chain too long')

    def __init__(self, path=None):
        from libzim.reader import Archive

        self.path = Path(path) if path else next((ROOT / 'knowledge/wiki/zim').glob('*.zim'))
        self.archive = Archive(str(self.path))
        receipt = self.path.with_suffix('.zim.provenance.json')
        self.receipt = (
            json.loads(receipt.read_text())
            if receipt.exists()
            else {'sha256': digest(self.path)}
        )

    def provenance(self, entry):
        self._entry(entry)
        metadata = {}
        for key in (
            'Title',
            'Date',
            'Language',
            'Creator',
            'Publisher',
            'Source',
            'Description',
        ):
            try:
                metadata[key] = self.archive.get_metadata(key).decode('utf-8')
            except (KeyError, RuntimeError):
                pass
        return {
            'archive': str(self.path),
            'sha256': self.receipt['sha256'],
            'entry': entry,
            'archive_metadata': metadata,
            'revision_id': None,
            'game_version_verified': False,
            'source_url': 'https://minecraft.wiki/w/' + entry.removeprefix('A/'),
            'license': 'See archive About/Copyright pages and attribution links; no revision ID inferred.',
        }

    def read(self, entry, section=None, fields=None):
        selected = ['text', 'headings'] if fields is None else fields
        if (
            not isinstance(selected, list)
            or not selected
            or any(v not in ('text', 'html', 'assets', 'links', 'headings') for v in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ValueError(
                'fields must be a nonempty unique list of text/html/assets/links/headings'
            )
        item = self._entry(entry).get_item()
        html = bytes(item.content).decode('utf-8', errors='replace')
        if section:
            # HTML heading IDs, rather than a fragile plain-text match.
            match = next(
                (
                    m
                    for m in re.finditer(r'<h([1-6])\b[^>]*>.*?</h\1>', html, re.S)
                    if re.search(r'\bid=["\']' + re.escape(section) + r'["\']', m.group())
                ),
                None,
            )
            if not match:
                raise KeyError(f'Section not found: {section}')
            end = re.search(r'<h[1-' + match.group(1) + r']\b', html[match.end() :])
            html = (
                html[match.start() : match.end() + end.start()]
                if end
                else html[match.start() :]
            )
        parser = PageParser()
        parser.feed(html)
        available = {
            'html': html,
            'text': ''.join(parser.text),
            'assets': parser.assets,
            'links': parser.links,
            'headings': parser.headings,
        }
        return {
            'entry': entry,
            **{key: available[key] for key in selected},
            'provenance': self.provenance(entry),
        }

    def asset(self, path):
        item = self._entry(path).get_item()
        self.information_only(item.path, item.title)
        return bytes(item.content), item.mimetype

    def search(self, query, language='en', limit=10):
        if language != 'en':
            raise ValueError('Only the downloaded English archive is configured')
        from libzim.search import Searcher, Query

        search = Searcher(self.archive).search(Query().set_query(query))
        results = []
        limit = min(max(limit, 1), 50)
        for result in search.get_results(0, min(1000, limit * 20)):
            path = result.path
            try:
                entry = self._entry(path)
                page = self.read(path)
            except PermissionError:
                continue
            results.append(
                {
                    'title': entry.title,
                    'entry': path,
                    'summary': page['text'][:600],
                    'match': 'libzim full-text index',
                    'provenance': page['provenance'],
                }
            )
            if len(results) >= limit:
                break
        return results


class SourceIndex:
    def __init__(self, database=None):
        self.path = Path(database or ROOT / 'knowledge/source-index.sqlite')
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def build(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                'CREATE VIRTUAL TABLE IF NOT EXISTS source USING fts5(side UNINDEXED, path UNINDEXED, text)'
            )
            db.execute('DELETE FROM source')
            for side in ('client', 'server'):
                root = ROOT / 'knowledge/minecraft/26.1/sources' / side
                for path in root.rglob('*.java'):
                    db.execute(
                        'INSERT INTO source VALUES(?,?,?)',
                        (
                            side,
                            str(path.relative_to(ROOT)),
                            path.read_text(encoding='utf-8', errors='replace'),
                        ),
                    )
            carpet = ROOT / 'vendor/fabric-carpet/src/main/java'
            if carpet.exists():
                for path in carpet.rglob('*.java'):
                    db.execute(
                        'INSERT INTO source VALUES(?,?,?)',
                        (
                            'mod:carpet',
                            str(path.relative_to(ROOT)),
                            path.read_text(encoding='utf-8', errors='replace'),
                        ),
                    )
            return db.execute('SELECT count(*) FROM source').fetchone()[0]

    def search(self, query, side=None, limit=10, mode='text'):
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Source query must contain text')
        if mode not in ('text', 'phrase', 'fts'):
            raise ValueError('mode must be text, phrase or fts')
        quote = lambda value: '"' + value.replace('"', '""') + '"'
        match = (
            query
            if mode == 'fts'
            else (
                quote(query)
                if mode == 'phrase'
                else ' AND '.join(quote(part) for part in query.split())
            )
        )
        with closing(sqlite3.connect(self.path)) as db:
            try:
                rows = db.execute(
                    'SELECT side,path,snippet(source,2,\'[\',\']\',\'…\',40) FROM source WHERE source MATCH ? AND (? IS NULL OR side=?) LIMIT ?',
                    (match, side, side, min(limit, 100)),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if mode == 'fts':
                    raise ValueError(
                        'Invalid FTS syntax; use mode=text for ordinary code/text queries: '
                        + str(exc)
                    ) from None
                raise
        return [
            {
                'side': s,
                'file': p,
                'snippet': t,
                'game_version': '26.1',
                'naming': 'mod source' if s.startswith('mod:') else 'official',
            }
            for s, p, t in rows
        ]

    def read(self, path, start=1, count=100):
        resolved = (ROOT / path).resolve()
        allowed = [
            (ROOT / 'knowledge/minecraft/26.1/sources').resolve(),
            (ROOT / 'vendor/fabric-carpet/src/main/java').resolve(),
        ]
        if not any(resolved.is_relative_to(root) for root in allowed):
            raise ValueError('Source path outside configured roots')
        lines = resolved.read_text(encoding='utf-8').splitlines()
        return {
            'file': str(resolved),
            'lines': [
                {'line': i + 1, 'text': line}
                for i, line in enumerate(lines)
                if start <= i + 1 < start + min(count, 500)
            ],
            'sha256': digest(resolved),
        }
