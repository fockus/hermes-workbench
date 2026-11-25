"""Run the pinned Workbench host's native static guard without discovering plugins or loading skills.

Invoke with the trusted Workbench prefix interpreter (-IB) and its trusted source directory.
Only manifests and files are read from this distribution. Results are not runtime activation.
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('wb_skill_assets', HERE/'install.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-source', type=Path, required=True)
    root = parser.parse_args().host_source.resolve(strict=True)
    d = a.read_doc()
    a.verify(d)
    policy = d['static_admission']
    for k, digest in policy['scanner_files'].items():
        a.require(hashlib.sha256((root/k).read_bytes()).hexdigest() == digest, 'host scanner drift: '+k)
    with tempfile.TemporaryDirectory(prefix='workbench-guard-') as tmp:
        work = Path(tmp).resolve()
        os.environ.update(HOME=str(work), HERMES_HOME=str(work/'profile'))
        sys.path.insert(0, str(root))
        from tools.skills_guard import SCANNER_VERSION, scan_skill, should_allow_install
        from tools.skills_guard_credentials import model_provider_key_names
        a.require(SCANNER_VERSION == policy['scanner_version'] and model_provider_key_names(), 'incomplete native guard')
        active = a.selected(d['skills'])
        observed = {}
        for n, s in d['skills'].items():
            data = a.pack_files(n, s, d)
            if data is None:
                continue
            material = s.get('edition', s)
            folder = work/'scan'/n
            for k, b in data.items():
                p = folder/k
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b)
                p.chmod(0o700 if k in material['executable'] else 0o600)
            a.checked_tree(folder, material)
            source, _, _, _ = a.bundle_source(n, s, d)
            result = scan_skill(folder, source=source, trust_level='community')
            allowed = should_allow_install(result)[0]
            expected = policy['observed'][n]
            a.require([result.verdict, allowed, n in active] == expected, 'admission changed: '+n)
            if n in active:
                a.require(result.verdict == 'safe' and allowed is True, 'unsafe selection: '+n)
            observed[n] = result.verdict
        a.require(observed.keys() == policy['observed'].keys(), 'scope differs')
        print(json.dumps({'version':SCANNER_VERSION,'scanned':len(observed),'verdicts':Counter(observed.values()),
                          'selected':len(active),'not_selected':sorted(observed.keys()-active.keys()),'executed_payloads':0},sort_keys=True))


if __name__ == '__main__':
    main()
