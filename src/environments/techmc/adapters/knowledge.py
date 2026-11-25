"""Read permitted pages from a local ZIM archive and search or retrieve locally indexed
game/mod source.
"""

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

from environments.techmc.paths import ASSET_ROOT, DATA_ROOT as ROOT
from environments.techmc.tools.bootstrap import digest


class PageParser(HTMLParser):
    """Extract text and document links from one HTML page or selected section.

    text is list[str], including line/tab markers for common layout tags.
    assets contains img attribute dictionaries, links contains href values,
    and headings contains IDs found on heading tags. hidden counts nested
    script/style tags so handle_data() omits their text. HTMLParser supplies
    decoded character references and attributes whose values may be None.

    Example:
        parser = PageParser()
        parser.feed(html)
        text = ''.join(parser.text)

    text is the extracted page text used by ZimWiki.read(); assets, links, and
    headings provide the other selectable page fields.
    """

    def __init__(self):
        """Initialize the page-field lists and hidden-text nesting count."""

        super().__init__(convert_charrefs=True)
        self.text, self.assets, self.links, self.headings = [], [], [], []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        """Collect tag metadata and text-layout markers, entering hidden script/style content
        as needed.
        """
        # attrs: list[tuple[str, str | None]] from HTMLParser; a is its attribute-name
        # dictionary.

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
        """Leave one hidden script/style nesting level."""

        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        """Append text content when outside script/style tags."""

        if not self.hidden:
            self.text.append(data)


class ZimWiki:
    """Read and search the downloaded English Wiki archive with entry provenance.

    path is the ZIM Path and archive is its libzim Archive handle. receipt is
    loaded from the adjacent provenance JSON when available, or contains the
    computed archive hash. _entry() checks the requested path/title and each
    redirect against the information-page namespace policy.

    read() selects text, HTML, assets, links, or heading IDs and can restrict the
    page to one heading section. asset() returns bytes and MIME type. search()
    uses the archive's full-text index, reads permitted results, and supplies
    text excerpts with archive provenance. provenance() reports stored archive
    metadata and hash alongside the requested entry.

    Example:
        wiki = ZimWiki()
        matches = wiki.search('hopper', limit=1)
        if matches:
            page = wiki.read(matches[0]['entry'], fields=['text', 'headings'])

    page contains the selected fields and archive provenance for the first
    permitted search result.
    """

    @staticmethod
    def information_only(path, title=''):
        """Normalize entry paths/titles and raise for tutorial namespace entries."""

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
        """Resolve a permitted archive entry, checking each redirect path and title."""

        self.information_only(path)
        entry = self.archive.get_entry_by_path(path)
        for _ in range(12):
            self.information_only(entry.path, entry.title)
            if not entry.is_redirect:
                return entry
            entry = entry.get_redirect_entry()
        raise ValueError('Wiki redirect chain too long')

    def __init__(self, path=None):
        """Open the selected or installed ZIM archive and load or compute its provenance
        hash.
        """

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
        """Return archive metadata and the stored content hash for a permitted entry."""

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
        """Extract selected page fields and provenance, optionally limited to a heading-ID
        section.
        """

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
        # The archive item exposes binary content; html is the decoded str parsed below.
        html = bytes(item.content).decode('utf-8', errors='replace')
        if section:
            # Select the requested heading by its HTML ID and stop at the next heading of equal
            # or higher level.
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
        """Read a permitted archive asset and return its bytes and MIME type."""

        item = self._entry(path).get_item()
        self.information_only(item.path, item.title)
        return bytes(item.content), item.mimetype

    def search(self, query, language='en', limit=10):
        """Search the English archive, skip excluded entries, and return page excerpts with
        provenance.
        """

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
    """Build and query the local FTS5 index of configured game and mod source files.

    path is the SQLite database Path. build() replaces its indexed rows with
    Java source from the installed Minecraft corpus and project-owned Carpet
    vendor tree. Rows retain side, relative file path, and source text.

    search() supports quoted literal terms, a literal phrase, or explicit FTS
    syntax. read() resolves a source file inside the configured corpus roots
    and returns numbered lines plus its SHA-256 hash.

    Example, using the prepared source index:
        source = SourceIndex()
        matches = source.search('hopper', side='server', limit=1)
        if matches:
            excerpt = source.read(matches[0]['file'], count=20)

    excerpt contains up to twenty numbered source lines and the file hash for
    the first matching server file.
    """

    def __init__(self, database=None):
        """Select the source-index database and create its parent directory."""

        self.path = Path(database or ROOT / 'knowledge/source-index.sqlite')
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def build(self):
        """Replace the FTS index with configured game/mod Java source rows and return their
        count.
        """

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
            carpet = ASSET_ROOT / 'vendor/fabric-carpet/src/main/java'
            if carpet.exists():
                for path in carpet.rglob('*.java'):
                    db.execute(
                        'INSERT INTO source VALUES(?,?,?)',
                        (
                            'mod:carpet',
                            str(path.relative_to(ASSET_ROOT)),
                            path.read_text(encoding='utf-8', errors='replace'),
                        ),
                    )
            return db.execute('SELECT count(*) FROM source').fetchone()[0]

    def search(self, query, side=None, limit=10, mode='text'):
        """Search the FTS index in literal-term, literal-phrase, or explicit-query mode."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError('Source query must contain text')
        if mode not in ('text', 'phrase', 'fts'):
            raise ValueError('mode must be text, phrase or fts')
        # Quote literal FTS terms and double embedded quotation marks before building the query
        # expression.
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
        """Return up to 500 numbered lines and a content hash for a file under the configured
        source roots.
        """
        # candidate becomes a Path; vendor-prefixed references resolve against project assets,
        # other references against data.

        candidate = Path(path)
        base = ASSET_ROOT if candidate.parts[:1] == ('vendor',) else ROOT
        resolved = (base / candidate).resolve()
        allowed = [
            (ROOT / 'knowledge/minecraft/26.1/sources').resolve(),
            (ASSET_ROOT / 'vendor/fabric-carpet/src/main/java').resolve(),
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
