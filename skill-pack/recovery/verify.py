"""Verify shipping bytes; optionally retrieve unopened OMH artifacts from the publisher."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def check(ok, message):
    if not ok:
        raise ValueError(message)


def digest(p):
    check(p.resolve(strict=True) == p and p.is_file(), f'not a regular source file: {p.name}')
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_assets(lock):
    manifest = json.loads((HERE.parent / 'manifest.json').read_bytes())
    released = {k for k, v in manifest['skills'].items() if v['delivery'] in ('owner-release','owner-reading')}
    check(released == lock['assembly'].keys(), 'consolidation list differs')
    notice = lock['orchestra']; root = HERE / notice['root']
    actual = {p.relative_to(root).as_posix(): digest(p) for p in root.rglob('*') if not p.is_dir()}
    check(actual == {k: v['sha256'] for k, v in notice['files'].items()}, 'publisher trees or notice differ')
    for row in lock['companions'].values():
        check(digest(HERE / row['path']) == row['sha256'], 'companion differs')
    overlays = ROOT / 'plugin-pack/base-overlays'
    for name, component in lock['components'].items():
        check(digest(ROOT / component['license']) == digest(ROOT / 'LICENSES' / ('Memory-Bank.txt' if name == 'memory-bank' else 'LLM-Wiki.txt')), 'component license differs')
        for k, rec in component['files_added_to_existing_overlay'].items():
            p = overlays / name / 'runtime-changes' / k
            check(digest(p) == rec['sha256'] and bool(p.stat().st_mode & 0o111) == (rec['mode'] == '100755'), f'component file differs: {name}/{k}')
    return len(actual)


def publisher_files(records, folder, fetch):
    if fetch:
        folder.mkdir(mode=0o700, exist_ok=False)
    for item in records:
        name = item['filename']; target = folder / name
        if fetch:
            with tempfile.TemporaryDirectory(dir=folder) as tmp:
                dest = Path(tmp) / 'download'
                subprocess.run(['curl', '-q', '--fail', '--silent', '--show-error', '--location', '--proto', '=https', '--proto-redir', '=https', '--max-time', '240', '--output', str(dest), item['url']], check=True)
                check(dest.stat().st_size == item['bytes'] and digest(dest) == item['sha256'], f'publisher bytes changed: {name}')
                dest.rename(target)
        check(digest(target) == item['sha256'] and target.stat().st_size == item['bytes'], f'wrong provider artifact: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--omh-cache', type=Path, help='verify previously downloaded publisher files')
    group.add_argument('--fetch-omh', type=Path, help='create a NEW directory for verified, unopened files only')
    group.add_argument('--fetch-held-source', type=Path, help='retrieve the unopened Superpowers publisher archive to a NEW directory; never installs the rejected variant')
    args = parser.parse_args()
    lock = json.loads((HERE / 'sources.json').read_bytes()); count = verify_assets(lock)
    folder = args.omh_cache or args.fetch_omh or args.fetch_held_source
    if folder:
        records = ([lock['held_publisher']] if args.fetch_held_source else [lock['omh'][k] for k in ('official_wheel', 'catalog_archive')])
        publisher_files(records, folder.expanduser().absolute(), args.omh_cache is None)
    print(json.dumps({'reading_files_including_notice_and_publisher_links': count, 'publisher_links_not_reproduced': sorted(k for k, v in lock['orchestra']['files'].items() if v.get('delivery') == 'publisher-link'), 'component_overlays': sorted(lock['components']), 'companions': sorted(lock['companions']), 'publisher_artifacts_checked': len(records) if folder else 0, 'installed': False}))


if __name__ == '__main__':
    main()
