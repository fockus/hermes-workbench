"""End-to-end test: create two new installations, seed ONLY the first, query both from the same cwd."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_mb import probe  # noqa: E402

RECIPE = Path(__file__).resolve().parents[1]


def exercise(first, second):
    # Called only with installations just made by this test. Never accept existing data on the CLI.
    token = 'WB' + uuid.uuid4().hex
    cwd = first / 'profile'  # contains BOTH tempting foreign stores for the second plugin host
    for root in (first, second):
        enabled = json.loads(subprocess.check_output([root / 'workbench', 'config', 'get', 'plugins.enabled', '--json'], cwd=cwd))
        assert set(enabled) == {'memory-bank', 'llm-wiki'}, enabled
        assert not list((root / 'profile/llm-wiki/raw').rglob('*.md'))
        probe(root, token, 'absent', cwd=cwd)
    bank = cwd / '.memory-bank'
    # The upstream context hook reads the first 30 lines; seed the synthetic status there.
    status = bank / 'status.md'
    status.write_text('# Synthetic status\n' + token + '\n\n' + status.read_text())
    wiki = cwd / 'llm-wiki'
    with (wiki / 'raw/synthetic.md').open('x') as out:
        out.write('# Synthetic\n\n' + token + '\n')
    subprocess.run([first / 'llm-wiki', 'index', '--root', wiki, '--lexical-only'], cwd=cwd, check=True)
    probe(first, token, 'present', cwd=cwd)
    probe(second, token, 'absent', cwd=cwd)
    print('PASS: distinct profile roots; record + handoff in A, no record or handoff in B from A’s cwd')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path, required=True, help='existing synthetic scratch parent')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(dir=args.parent.resolve(), prefix='workbench-dual-') as tmp:
        base = Path(tmp)
        (base / 'home').mkdir()
        (base / 'tmp').mkdir()
        env = dict(HOME=str(base / 'home'), HERMES_HOME=str(base / 'home/profile'), TMPDIR=str(base / 'tmp'),
                   PATH=os.environ['PATH'])
        first, second = base / 'alpha', base / 'beta'
        for dest, flags in ((first, ['--with-tui']), (second, [])):
            subprocess.run([sys.executable, '-I', RECIPE / 'install.py', '--prefix', dest, *flags], cwd=base,
                           env=env, check=True)
        exercise(first, second)
        for dest, flags in ((first, ['--with-tui']), (second, [])):
            subprocess.run([sys.executable, '-I', RECIPE / 'install.py', '--prefix', dest, '--check', *flags],
                           env=env, cwd=base, check=True)


if __name__ == '__main__':
    main()
