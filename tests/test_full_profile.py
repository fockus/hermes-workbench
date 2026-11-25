"""Release routing, not plugin, skill or external CLI functionality."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = runpy.run_path(str(ROOT / 'install.py'))


class FullProfile(unittest.TestCase):
    def test_accounting_is_not_an_activation_receipt(self):
        self.assertIn('full_selection', BASE, 'full-profile must account for excluded originals')
        account = BASE['full_selection']()
        self.assertEqual(account['missing_skill_originals'], [])
        self.assertEqual(account['selected_skill_count'], 57)
        self.assertEqual(account['reading_only_skill_names'], ['mlops-model-lifecycle', 'systematic-debugging'])
        self.assertEqual(account['not_selected_skill_variants'], ['mlops-model-lifecycle', 'requesting-code-review', 'systematic-debugging', 'test-driven-development'])
        self.assertIn('references/lm-evaluation-harness/benchmark-guide.md', account['not_redistributed_verbatim']['mlops-model-lifecycle'])
        self.assertEqual(account['protected_skill_names'], ['requesting-code-review', 'systematic-debugging', 'test-driven-development'])
        self.assertEqual(account['plugins_not_requested'], ['omh', 'soul-composer', 'workflows'])
        self.assertEqual(len(account['plugins_requested_without_enable']), 26)
        self.assertEqual(account['plugin_owned_skill_visibility'], 'not verified')
        self.assertFalse(account['activation_verified'])

    def test_help_and_incompatible_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'fresh'
            r = subprocess.run([sys.executable, '-IB', ROOT/'install.py', '--full-profile', '--wiki', '--prefix', p], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('cannot combine', r.stderr)
            self.assertFalse(p.exists())

    def test_canonical_config_and_launcher_route(self):
        # The endpoint is a temporary env/argv recorder, never Hermes, node or an executor.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            for n in ('profile', 'tool-bin', 'venv/bin', 'tmp', 'home'):
                (root/n).mkdir(parents=True)
            BASE['full_config'](root, True)
            c = json.loads((root/'profile/config.yaml').read_text())
            self.assertEqual(c['skills']['external_dirs'], [str(root/'profile/external-skills')])
            self.assertFalse(c['plugins']['enabled'])
            self.assertEqual(c['display']['skin'], 'claude-graphite')
            launch = BASE['copy_launcher'](root, 'workbench')
            probe = root/'venv/bin/hermes'
            probe.write_text('#!/bin/sh\n/usr/bin/env\nprintf "args=%s\\n" "$*"\n')
            probe.chmod(0o700)
            (root/'tool-bin/node').symlink_to('/usr/bin/false')
            env = {'HOME':'/not-selected', 'HERMES_HOME':'/other-profile', 'SENTINEL':'must-not-leak', 'PATH':'/usr/bin:/bin'}
            out = subprocess.check_output([launch, '--tui'], cwd=root, env=env, text=True)
            observed = dict(x.split('=', 1) for x in out.splitlines())
            for key, rel in [('HOME','home'), ('HERMES_HOME','profile'), ('HERMES_TUI_DIR','tui'), ('WORKBENCH_MB_RUNTIME','plugin-pack/runtime/memory-bank')]:
                self.assertEqual(observed[key], str(root/rel))
            self.assertEqual(observed['args'], '--tui')
            self.assertNotIn('SENTINEL', observed)
            self.assertFalse((root/'plugin-pack/profile').exists())
            (root/BASE['PENDING']).touch()
            partial = subprocess.run([launch, '--tui'], cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(partial.returncode, 0)
            self.assertIn('Incomplete full-profile', partial.stderr)
            self.assertFalse(partial.stdout)

    def test_receipt_only_after_both_entrypoints(self):
        from unittest.mock import patch
        f = BASE['finish_full']
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root/BASE['PENDING']).touch()
            calls = []
            def record(group, args, env, cwd):
                calls.append((group, args))
                if group == 'plugin-pack':
                    raise subprocess.CalledProcessError(1, group)
            with patch.dict(f.__globals__, pack_command=record):
                with self.assertRaises(subprocess.CalledProcessError):
                    f(root, {})
            self.assertEqual(calls, [('skill-pack', ['install', '--prefix', root]),
                                     ('plugin-pack', ['install', 'all', '--profile', 'integrated', '--prefix', root])])
            self.assertTrue((root/BASE['PENDING']).is_file())
            self.assertFalse((root/BASE['FULL_RECEIPT']).exists())
