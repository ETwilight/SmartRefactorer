"""Local wikitext corpus: policy-aware API fetch, XML import, SQLite FTS search.

Python standard library only. Templates/images are not expanded or downloaded.
"""

import argparse
import bz2
from datetime import datetime, timezone
import gzip
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET


def now():
    return datetime.now(timezone.utc).isoformat()


def database(out):
    out.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(out / 'index.sqlite')
    db.execute(
        'CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(title, content, path UNINDEXED, revision UNINDEXED, url UNINDEXED)'
    )
    return db


def save(out, db, record):
    key = hashlib.sha256(record['title'].encode()).hexdigest()[:24]
    revision = str(record.get('revision_id') or 'unknown')
    path = out / 'pages' / f'{key}-{revision}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    with db:
        db.execute('DELETE FROM pages WHERE title = ?', (record['title'],))
        db.execute(
            'INSERT INTO pages VALUES(?,?,?,?,?)',
            (record['title'], record['wikitext'], str(path.resolve()), revision, record['url']),
        )
    return str(path.resolve())


def get(url, agent):
    request = urllib.request.Request(
        url, headers={'User-Agent': agent, 'Accept': 'application/json,text/plain'}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        # Cross-origin redirects must not silently change the policy target.
        if urllib.parse.urlsplit(response.url).netloc != urllib.parse.urlsplit(url).netloc:
            raise RuntimeError(
                'Cross-origin redirect; inspect the official endpoint before continuing'
            )
        return response.read().decode('utf-8')


def policy_allows(text, agent, url):
    """Handle robots wildcards/$ (stdlib RobotFileParser treats them literally)."""
    groups, agents, rules = [], [], []
    for raw in text.splitlines() + ['User-agent: __end__']:
        line = raw.split('#', 1)[0].strip()
        if ':' not in line:
            continue
        key, value = (x.strip() for x in line.split(':', 1))
        key = key.lower()
        if key == 'user-agent':
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in ('allow', 'disallow') and agents:
            rules.append((key, value))
    matches = []
    for names, entries in groups:
        specificity = max(
            (
                0 if name == '*' else len(name)
                for name in names
                if name == '*' or name in agent.lower()
            ),
            default=-1,
        )
        if specificity >= 0:
            matches.append((specificity, entries))
    if not matches:
        return True
    best_agent = max(score for score, _ in matches)
    parts = urllib.parse.urlsplit(url)
    target = parts.path + ('?' + parts.query if parts.query else '')
    applicable = []
    for score, entries in matches:
        if score != best_agent:
            continue
        for key, pattern in entries:
            if not pattern:
                continue
            end = pattern.endswith('$')
            body = pattern[:-1] if end else pattern
            regex = (
                '^'
                + '.*'.join(re.escape(piece) for piece in body.split('*'))
                + ('$' if end else '')
            )
            if re.search(regex, target):
                applicable.append((len(body.replace('*', '')), key == 'allow'))
    return max(applicable, default=(0, True))[1]


def fetch(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    api = args.api
    parts = urllib.parse.urlsplit(api)
    if parts.scheme != 'https' or parts.query or parts.fragment:
        raise ValueError('--api must be an HTTPS endpoint without query/fragment')
    agent = f'TechnicalMinecraftResearchBot/0.1 (AI research; {args.contact})'
    report = {
        'started_at': now(),
        'api': api,
        'downloaded': [],
        'skipped': [],
        'missing': [],
        'status': 'started',
    }
    db = None
    try:
        robots_url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, '/robots.txt', '', '')
        )
        robots_text = get(robots_url, agent)
        (out / 'robots.txt').write_text(robots_text, encoding='utf-8')
        robots = urllib.robotparser.RobotFileParser(robots_url)
        robots.parse(robots_text.splitlines())
        if not policy_allows(robots_text, agent, api):
            raise RuntimeError(
                'robots.txt disallows this API path for this agent; use an authorized export'
            )
        delay = max(5.0, args.delay, robots.crawl_delay(agent) or 0)
        rate = robots.request_rate(agent)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        titles = [
            x.strip()
            for x in Path(args.titles).read_text(encoding='utf-8-sig').splitlines()
            if x.strip() and not x.lstrip().startswith('#')
        ]
        if len(titles) > args.max_pages:
            raise ValueError(
                'Seed list exceeds --max-pages; choose a bounded collection explicitly'
            )
        db = database(out)
        state_file = out / 'completed.json'
        completed = (
            json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
        )
        for title in titles:
            if not args.refresh and title in completed and Path(completed[title]).is_file():
                report['skipped'].append(title)
                continue
            params = {
                'action': 'query',
                'prop': 'revisions',
                'titles': title,
                'redirects': '1',
                'rvprop': 'ids|timestamp|content',
                'rvslots': 'main',
                'format': 'json',
                'formatversion': '2',
                'maxlag': '5',
            }
            url = api + '?' + urllib.parse.urlencode(params)
            if not policy_allows(robots_text, agent, url):
                raise RuntimeError('robots.txt disallows the requested query')
            time.sleep(delay)
            data = json.loads(get(url, agent))
            if 'error' in data:
                raise RuntimeError(f"API error: {data['error']}")
            for page in data['query']['pages']:
                if page.get('missing') or not page.get('revisions'):
                    report['missing'].append(title)
                    continue
                revision = page['revisions'][0]
                text = revision['slots']['main']['content']
                base = f'{parts.scheme}://{parts.netloc}'
                record = {
                    'title': page['title'],
                    'requested_title': title,
                    'page_id': page['pageid'],
                    'revision_id': revision['revid'],
                    'revision_timestamp': revision['timestamp'],
                    'retrieved_at': now(),
                    'url': base
                    + '/w/'
                    + urllib.parse.quote(page['title'].replace(' ', '_'), safe='/:'),
                    'history_url': base
                    + '/index.php?'
                    + urllib.parse.urlencode({'title': page['title'], 'action': 'history'}),
                    'license_source': base + '/w/Minecraft_Wiki:About',
                    'game_version_applicability': 'unverified',
                    'source_api': api,
                    'wikitext': text,
                }
                completed[title] = save(out, db, record)
                state_file.write_text(
                    json.dumps(completed, ensure_ascii=False, indent=2), encoding='utf-8'
                )
                report['downloaded'].append(page['title'])
                print(page['title'])
        report['status'] = 'complete'
    except Exception as exc:
        report['status'] = 'blocked'
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if db:
            db.close()
        report['finished_at'] = now()
        (out / 'acquisition_status.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
        )


def import_xml(args):
    out = Path(args.out)
    db = database(out)
    source = Path(args.file)
    opener = (
        bz2.open if source.suffix == '.bz2' else gzip.open if source.suffix == '.gz' else open
    )
    count = 0
    try:
        with opener(source, 'rb') as stream:
            events = ET.iterparse(stream, events=('start', 'end'))
            _, root = next(events)
            for event, element in events:
                if event != 'end' or element.tag.split('}')[-1] != 'page':
                    continue

                def read(parent, name):
                    child = parent.find('{*}' + name)
                    return child.text if child is not None and child.text else ''

                # A history dump may have many revisions; retain the newest timestamp.
                revisions = element.findall('{*}revision')
                if revisions:
                    revision = max(revisions, key=lambda r: read(r, 'timestamp'))
                    text = read(revision, 'text')
                    if not text:
                        text_node = revision.find('.//{*}slot/{*}text')
                        text = text_node.text or '' if text_node is not None else ''
                    title = read(element, 'title')
                    record = {
                        'title': title,
                        'page_id': read(element, 'id'),
                        'revision_id': read(revision, 'id'),
                        'revision_timestamp': read(revision, 'timestamp'),
                        'retrieved_at': now(),
                        'url': args.wiki_url.rstrip('/')
                        + '/w/'
                        + urllib.parse.quote(title.replace(' ', '_'), safe='/:'),
                        'source_file': str(source.resolve()),
                        'game_version_applicability': 'unverified',
                        'license_source': args.wiki_url.rstrip('/') + '/w/Minecraft_Wiki:About',
                        'history_url': args.wiki_url.rstrip('/')
                        + '/index.php?'
                        + urllib.parse.urlencode({'title': title, 'action': 'history'}),
                        'wikitext': text,
                    }
                    save(out, db, record)
                    count += 1
                root.clear()
        print(f'Imported {count} pages; raw wikitext, templates not expanded')
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fetcher = commands.add_parser('fetch')
    fetcher.add_argument('--api', default='https://minecraft.wiki/api.php')
    fetcher.add_argument('--titles', required=True)
    fetcher.add_argument('--out', required=True)
    fetcher.add_argument('--contact', required=True)
    fetcher.add_argument('--delay', type=float, default=5)
    fetcher.add_argument('--max-pages', type=int, default=50)
    fetcher.add_argument('--refresh', action='store_true')
    importer = commands.add_parser('import-xml')
    importer.add_argument('--file', required=True)
    importer.add_argument('--out', required=True)
    importer.add_argument('--wiki-url', default='https://minecraft.wiki')
    search = commands.add_parser('search')
    search.add_argument('--out', required=True)
    search.add_argument('--query', required=True, help='SQLite FTS5 query syntax')
    search.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    if args.command == 'fetch':
        fetch(args)
    elif args.command == 'import-xml':
        import_xml(args)
    else:
        db_path = Path(args.out) / 'index.sqlite'
        if not db_path.is_file():
            raise RuntimeError('No corpus index; fetch or import pages first')
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                'SELECT title, snippet(pages,1,\'[\',\']\',\' … \',24), path, revision, url FROM pages WHERE pages MATCH ? ORDER BY rank LIMIT ?',
                (args.query, args.limit),
            )
            for row in rows:
                print(
                    json.dumps(
                        dict(zip(('title', 'snippet', 'path', 'revision_id', 'url'), row)),
                        ensure_ascii=False,
                    )
                )


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
