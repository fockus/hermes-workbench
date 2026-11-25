"""Public distribution contract (no network, provider, or local Hermes)."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GROUP = ROOT / 'review-skills'


class ReviewSkills(unittest.TestCase):
    def test_public_list_and_selection(self):
        cmd = [sys.executable, '-IB', GROUP / 'review.py']
        listing = subprocess.run([*cmd, 'list'], capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(set(listing.stdout.split()), {
            'brainstorming', 'diagnosing-superpowers', 'dispatching-parallel-agents', 'using-git-worktrees',
            'using-superpowers', 'writing-plans', 'finishing-a-development-branch',
            'receiving-code-review', 'verification-before-completion'})
        with tempfile.TemporaryDirectory() as tmp:
            for flags in (['--skill', '../untrusted'], ['--all', '--skill', 'brainstorming']):
                bad = subprocess.run([*cmd, 'install', '--prefix', tmp, *flags], capture_output=True)
                self.assertEqual(bad.returncode, 2)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_complete_public_trees_and_notice(self):
        self.assertTrue((GROUP / 'pins.json').is_file(), 'missing reviewed export')
        pins = json.loads((GROUP / 'pins.json').read_text())
        self.assertEqual(pins['repo'], 'obra/superpowers')
        self.assertEqual(pins['commit'], '22f0b463e7a5e579904dd6211a520321035526f7')
        payload = GROUP / 'payload'
        self.assertEqual({p.name for p in payload.iterdir()}, set(pins['skills']))
        for name, spec in pins['skills'].items():
            directory = payload / name
            self.assertFalse(directory.is_symlink())
            self.assertFalse(any(f.is_symlink() for f in directory.rglob('*')))
            files = {f.relative_to(directory).as_posix(): f for f in directory.rglob('*') if f.is_file()}
            self.assertEqual({k: hashlib.sha256(f.read_bytes()).hexdigest() for k, f in files.items()}, spec['files'])
            self.assertEqual(sorted(k for k, f in files.items() if f.stat().st_mode & 0o111), spec['executable'])
            self.assertIn(f'\nname: {name}\n', files['SKILL.md'].read_text())
        # Independently reconstruct actual Git trees (includes relative paths and executable bits).
        import os
        with tempfile.TemporaryDirectory() as tmp:
            git = ['git', '--git-dir', str(Path(tmp, 'objects.git')), '--work-tree', str(payload)]
            env = {'HOME': tmp, 'PATH': os.defpath, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
            subprocess.run([*git, 'init', '--quiet', '--template='], check=True, cwd=payload, env=env)
            subprocess.run([*git, 'add', '--', '.'], check=True, cwd=payload, env=env)
            tree = subprocess.check_output([*git, 'write-tree'], cwd=payload, env=env, text=True).strip()
            for name, spec in pins['skills'].items():
                actual = subprocess.check_output([*git, 'rev-parse', f'{tree}:{name}'], cwd=payload, env=env, text=True)
                self.assertEqual(actual.strip(), spec['tree'])
        self.assertEqual(hashlib.sha256((ROOT / 'LICENSES/Superpowers.txt').read_bytes()).hexdigest(), pins['license_sha256'])

    def test_aggregation_never_publishes_names_or_values(self):
        from runpy import run_path
        self.assertTrue((GROUP / 'aggregate.py').exists(), 'missing aggregate projection')
        summarize = run_path(str(GROUP / 'aggregate.py'))['summarize']
        def row(kind, name, state):
            return dict(classification='required missing', gap='profile-selection-not-reproduced',
                        layer='overlay', kind=kind, name=name, state=state)
        rows = [row('setting', 'display.marker', 'private-value'),
                row('toolset-selection', 'private-route.hidden', 'selected'),
                row('setting', 'absent.marker', 'inherit-or-host-default')]
        report = summarize(rows, {'display': {'marker': 'private-value'}})
        self.assertEqual(report['settings_equal'], 1)
        self.assertEqual(report['selections_equal'], 0)
        self.assertNotIn('private', json.dumps(report))
        self.assertNotIn('hidden', json.dumps(report))

    def test_non_workbench_is_rejected_before_any_write(self):
        self.assertTrue((GROUP / 'review.py').is_file(), 'missing installer')
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run([sys.executable, '-IB', GROUP / 'review.py', 'install', '--prefix', tmp], capture_output=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])
