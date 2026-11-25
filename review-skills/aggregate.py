"""Local comparison only. Publish counts, never baseline keys, routes or config values."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

GAPS = ('no-release-component', 'profile-selection-not-reproduced', 'runtime-not-demonstrated',
        'registration-not-reproduced')


def summarize(rows, config):
    missing = [r for r in rows if r['classification'] == 'required missing']
    counts = {g: sum(r['gap'] == g for r in missing) for g in GAPS}
    if sum(counts.values()) != len(missing):
        raise ValueError('unknown gap type')
    equal = selections = plugin_settings = 0
    unset = object()
    for r in missing:
        if r['gap'] != 'profile-selection-not-reproduced':
            continue
        key, _, tail = r['name'].partition('.')
        expected = r['state']
        if r['kind'] == 'toolset-selection':
            value = config.get('toolsets') if key == 'global' else (config.get('platform_toolsets') or {}).get(key)
            selections += expected == 'selected' and isinstance(value, list) and tail in value
        elif r['kind'] == 'setting' and key == 'plugin':
            value = (config.get('plugins') or {}).get(expected)
            plugin_settings += expected in ('enabled', 'disabled') and isinstance(value, list) and tail in value
        elif r['kind'] == 'setting':
            value = config
            for field in r['name'].split('.'):
                value = value.get(field, unset) if isinstance(value, dict) else unset
            if value is unset:
                continue  # missing key is not proof of inheritance
            if value in ('', None) and expected == 'inherit-or-host-default':
                equal += 1
            elif type(value) in (bool, int, str):
                equal += (str(value).lower() if type(value) is bool else str(value)) == expected
    return dict(rows=len(rows), required_missing=len(missing), by_gap=counts, settings_equal=equal,
                selections_equal=selections, plugin_choices_equal=plugin_settings)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--prefix', required=True, type=Path)
    p.add_argument('--baseline', required=True, type=Path)
    args = p.parse_args()
    here = Path(__file__).resolve()
    sys.path.insert(0, str(here.parent))
    from review import validate_prefix
    root = args.prefix.absolute()
    validate_prefix(root)
    # The host reads only its isolated configuration. The private reference never goes to the host.
    # Keep the observed projection in a private pipe; neither input is a public artifact.
    code = ('import json; from hermes_cli.config import load_config; c=load_config(); '
            'print(json.dumps({k:c.get(k) for k in '
            '("agent","auxiliary","context","delegation","display","memory","model","plugins",'
            '"toolsets","platform_toolsets")}))')
    cfg = subprocess.check_output([root / 'venv/bin/python', '-IB', '-c', code],
                                  env={'HOME': str(root / 'home'), 'HERMES_HOME': str(root / 'profile'),
                                       'PATH': '/usr/bin:/bin', 'TMPDIR': str(root / 'tmp')},
                                  cwd=root / 'home', timeout=30, stderr=subprocess.DEVNULL)
    baseline = json.loads(args.baseline.read_bytes())
    rows = baseline['rows']
    if (baseline.get('format') != 'workbench-parity-v1' or not rows or
            len({(r['kind'], r['layer'], r['name']) for r in rows}) != len(rows)):
        raise ValueError('invalid reference')
    print(json.dumps(summarize(rows, json.loads(cfg)), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        sys.exit('Local comparison failed; inputs not echoed.')
