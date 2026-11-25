"""Offline review-skills add-on for an explicitly selected, trusted Workbench."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def require(ok, msg):
    if not ok:
        raise ValueError(msg)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_prefix(root):
    require(root.is_absolute() and root == root.resolve(strict=True), 'noncanonical prefix')
    receipt = root / '.workbench-installed.json'
    require(receipt.is_file() and not receipt.is_symlink(), 'expected a Workbench receipt')
    record = json.loads(receipt.read_bytes())
    lock = (HERE.parent / 'workbench.lock.json').read_bytes()
    require(record.get('prefix') == str(root) and record.get('mode') in {'base', 'tui'}
            and record.get('lock') == digest(lock), 'different Workbench installation')
    for part in ('source', 'home', 'tmp', 'profile', 'venv', 'venv/bin'):
        path = root / part
        require(path.resolve(strict=True) == path and path.is_dir(), 'redirected prefix directory')
    commit = subprocess.check_output(['git', '--no-lazy-fetch', '-C', root / 'source', 'rev-parse', 'HEAD'],
                                     env={'PATH': os.defpath, 'GIT_CONFIG_GLOBAL': os.devnull,
                                          'GIT_CONFIG_NOSYSTEM': '1'}, text=True).strip()
    require(commit == json.loads(lock)['commit'], 'unexpected host commit')


DEFAULT = ('finishing-a-development-branch', 'receiving-code-review', 'verification-before-completion')


def invoke(root, base, action, names, *, capture=False):
    # Use only the prefix interpreter, never user site, shell startup or caller's profile.
    env = dict(HOME=str(base / 'home'), HERMES_HOME=str(base / 'profile'), TMPDIR=str(base / 'tmp'),
               PATH=f'{root}/tool-bin:/usr/bin:/bin', LANG='C.UTF-8')
    return subprocess.run([root / 'venv/bin/python', '-IB', HERE / 'worker.py', action, str(root), *names],
                          env=env, cwd=base / 'home', check=True, timeout=90, capture_output=capture)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    available = tuple(json.loads((HERE / 'pins.json').read_bytes())['skills'])
    p.add_argument('action', choices=['list', 'install', 'check', 'smoke'])
    p.add_argument('--prefix', type=Path, help='absolute path to your Workbench base/TUI installation')
    pick = p.add_mutually_exclusive_group()
    pick.add_argument('--skill', action='append', choices=available, help='select one skill (repeatable)')
    pick.add_argument('--all', action='store_true', help='select the whole public package (default: review trio)')
    args = p.parse_args()
    if args.action == 'list':
        if args.prefix or args.skill or args.all:
            p.error('list takes no options')
        print(*available, sep='\n')
        return
    if not args.prefix or not args.prefix.is_absolute():
        p.error('--prefix must be an absolute path')
    names = available if args.all else tuple(dict.fromkeys(args.skill or DEFAULT))
    root = args.prefix
    validate_prefix(root)
    if args.action != 'smoke':
        invoke(root, root, args.action, names)
        return
    with tempfile.TemporaryDirectory(prefix='review-smoke-', dir=root / 'tmp') as scratch:
        base = Path(scratch)
        for name in ('profile', 'home', 'tmp'):
            (base / name).mkdir(mode=0o700)
        for action in ('absent', 'install', 'check', 'install', 'check'):
            invoke(root, base, action, names)
        remaining = sorted(set(available) - set(names))
        if remaining:
            invoke(root, base, 'absent', remaining)
        # Edit only this disposable fixture. A repeat must refuse rather than repair user bytes.
        name = names[0]
        f = base / 'profile/skills' / name / 'SKILL.md'
        changed = f.read_bytes() + b'\nWorkbenchLocalEdit\n'
        f.write_bytes(changed)
        try:
            invoke(root, base, 'install', names, capture=True)
        except subprocess.CalledProcessError as failure:
            require(b'installed payload drift' in failure.stderr and f.read_bytes() == changed,
                    'unexpected drift response')
            print('PASS: repeat refused modified content; bytes preserved.')
        else:
            raise ValueError('modified fixture was accepted')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f'Review skills: {exc}')
