"""Range acquisition for mirrors that stall on multi-GB single responses."""

from pathlib import Path as _ToolPath
import sys as _ToolSys

_ToolSys.path.insert(0, str(_ToolPath(__file__).resolve().parents[3]))
from environments.techmc.tools.bootstrap import *
import concurrent.futures


def main():
    url = 'https://download.kiwix.org/zim/other/minecraft.wiki_en_all_2026-08.zim'
    target = ROOT / 'knowledge/wiki/zim/minecraft.wiki_en_all_2026-08.zim'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return download(url, target)
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={'Range': 'bytes=0-0', 'User-Agent': UA}),
        timeout=60,
    ) as response:
        if response.status != 206 or not response.headers.get('Content-Range', '').startswith(
            'bytes 0-0/'
        ):
            raise RuntimeError('Mirror does not support ranges')
        mirror = response.geturl()
        total = int(response.headers['Content-Range'].split('/')[-1])
        etag = response.headers.get('ETag')
        modified = response.headers.get('Last-Modified')
        response.read()
    # Bind every independently cached chunk to the same archive validator.
    directory = target.parent / (target.name + '.chunks')
    directory.mkdir(exist_ok=True)
    descriptor = {
        'url': url,
        'mirror': mirror,
        'size': total,
        'etag': etag,
        'modified': modified,
    }
    descriptor_file = directory / 'manifest.json'
    if descriptor_file.exists() and json.loads(descriptor_file.read_text()) != descriptor:
        raise RuntimeError('Archive validator changed; preserve old chunks for review')
    write_json(descriptor_file, descriptor)
    chunk_size = 4 * 1024 * 1024

    def get_chunk(index):
        start = index * chunk_size
        end = min(total - 1, start + chunk_size - 1)
        path = directory / f'{index:05d}'
        if path.exists():
            receipt = path.with_suffix('.sha256')
            if (
                path.stat().st_size == end - start + 1
                and receipt.exists()
                and digest(path) == receipt.read_text()
            ):
                return path
            raise ValueError(f'Unverified existing chunk: {path}')
        for attempt in range(5):
            try:
                headers = {
                    'Range': f'bytes={start}-{end}',
                    'If-Range': etag or modified,
                    'User-Agent': UA,
                    'Accept-Encoding': 'identity',
                }
                with urllib.request.urlopen(
                    urllib.request.Request(mirror, headers=headers), timeout=45
                ) as r:
                    if (
                        r.status != 206
                        or r.headers.get('Content-Range') != f'bytes {start}-{end}/{total}'
                        or (etag and r.headers.get('ETag') != etag)
                    ):
                        raise RuntimeError('Archive range/validator mismatch')
                    part = path.with_suffix('.part')
                    with part.open('wb') as f:
                        shutil.copyfileobj(r, f, 64 * 1024)
                if part.stat().st_size != end - start + 1:
                    raise RuntimeError('Truncated chunk')
                part.replace(path)
                path.with_suffix('.sha256').write_text(digest(path))
                if index % 20 == 0:
                    print(f'ZIM chunk {index+1}/{(total+chunk_size-1)//chunk_size}', flush=True)
                return path
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2**attempt)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        chunks = list(pool.map(get_chunk, range((total + chunk_size - 1) // chunk_size)))
    part = target.with_suffix('.zim.part')
    with part.open('wb') as out:
        for chunk in chunks:
            with chunk.open('rb') as source:
                shutil.copyfileobj(source, out, 1024 * 1024)
    if part.stat().st_size != total:
        raise RuntimeError('Assembly size mismatch')
    sha = digest(part)
    part.replace(target)
    write_json(
        target.with_suffix('.zim.provenance.json'),
        {
            **descriptor,
            'sha256': sha,
            'acquired_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'publisher_checksum': None,
            'verification': 'HTTP range validators + local SHA256; not an official signature',
        },
    )
    # Individually verified chunks are redundant after atomic assembly.
    for chunk in chunks:
        chunk.unlink()
        chunk.with_suffix('.sha256').unlink()
    print(f'ZIM complete: {total} bytes SHA256 {sha}', flush=True)


if __name__ == '__main__':
    main()
