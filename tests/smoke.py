"""Offline Workbench acceptance: query the real Hermes CLI and its plugin probe."""
import json
from pathlib import Path
import re
import subprocess
import sys


def smoke(root):
    launcher = root / 'workbench'

    def cli(*args):
        return subprocess.check_output([launcher, *args], cwd=root / 'home', text=True, timeout=90)

    listing = cli('skin', 'list')
    if not re.search(r'^\* claude-graphite\s+user\s+Warm text and terracotta on graphite$', listing, re.M):
        raise SystemExit('Graphite is not the selected, user-installed skin')
    plugin = root / 'source/plugins/model-providers/arcee'
    report = json.loads(cli('plugins', 'validate', str(plugin), '--json'))
    probes = {c['name']: c for c in report['checks']}
    if not (report['ok'] and probes['security scan']['detail'] == 'safe'
            and probes['capability probe']['detail'] == 'import registered provider(s): arcee'):
        raise SystemExit('Bundled provider did not pass the real registration probe: ' + str(report))
    print('PASS: active Graphite skin; Hermes imported and registered the bundled arcee provider (no model call).')


if __name__ == '__main__':
    smoke(Path(sys.argv[1]).resolve())
