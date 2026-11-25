"""Standalone Wiki functional acceptance. Model download requires a separate explicit step."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def smoke(prefix, semantic):
    cli = prefix / 'llm-wiki'
    with tempfile.TemporaryDirectory(dir=prefix / 'tmp') as temp:
        root = Path(temp) / 'wiki'

        def call(*args):
            return json.loads(subprocess.check_output([cli, *args, '--root', root], cwd=temp, text=True, timeout=120))

        call('init', '--title', 'Synthetic acceptance only')
        source = Path(temp) / 'sample.md'
        source.write_text('# Synthetic source\n\nLarchBeacon is the fixture keyword.\n')
        call('ingest', source)
        mode = 'hybrid' if semantic else 'fulltext'
        call('index', *([] if semantic else ['--lexical-only']))
        state = call('status')
        assert state['indexed'] and not state['stale']
        assert bool(state['dimension']) == semantic
        hits = call('search', 'LarchBeacon', '--mode', mode, '--hops', '0')['hits']
        assert hits and any('LarchBeacon' in call('read', h['path'])['text'] for h in hits)
        assert call('lint')['ok']
        print('PASS: standalone init, ingest, index, status,', mode, 'search, read and lint; dimension:', state['dimension'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefix', type=Path)
    parser.add_argument('--semantic', action='store_true', help='exercise a model already explicitly downloaded')
    args = parser.parse_args()
    smoke(args.prefix.resolve(), args.semantic)
