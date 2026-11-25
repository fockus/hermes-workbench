"""Acceptance boundaries that do not require network access or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Components(unittest.TestCase):
    def test_public_pins_and_notices(self):
        pins = json.loads((ROOT / 'workbench.lock.json').read_text()).get('components', {})
        for name, pin in [('memory-bank', 'c0e56506df71bd1d066ff77870382797a4bf611b'),
                          ('llm-wiki', '99fea439401fcd079150db571a4650250a47943a')]:
            self.assertIn(name, pins)
            self.assertEqual(pins[name]['commit'], pin)
            self.assertIn('Copyright (c) 2026 Anton Ivanov', (ROOT / pins[name]['license']).read_text())

    def test_filtered_export_never_fetches_an_unlisted_bank_blob(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('installer', ROOT / 'install.py')
        assert spec and spec.loader
        recipe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recipe)
        self.assertTrue(hasattr(recipe, 'checkout_component'), 'missing filtered checkout')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            env = recipe.environment(tmp, tmp)
            source = tmp / 'upstream'
            source.mkdir()

            def git(cwd, *args):
                return subprocess.check_output(['git', *args], cwd=cwd, env=env, text=True).strip()

            git(source, 'init', '--quiet', '--template=')
            (source / 'LICENSE').write_text('synthetic license')
            (source / '.memory-bank').mkdir()
            (source / '.memory-bank/record').write_text('excluded synthetic blob')
            git(source, 'add', '--', 'LICENSE', '.memory-bank')
            git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
            excluded = git(source, 'rev-parse', 'HEAD:.memory-bank/record')
            tree = tmp / 'export'
            pin = dict(upstream=source.as_uri(), commit=git(source, 'rev-parse', 'HEAD'), paths=['LICENSE'])
            with self.assertRaisesRegex(ValueError, 'blob filtering'):
                recipe.checkout_component(tmp / 'unsupported', 'fixture', pin, env)
            git(source, 'config', 'uploadpack.allowFilter', 'true')
            recipe.checkout_component(tree, 'fixture', pin, env)
            self.assertEqual((tree / 'LICENSE').read_text(), 'synthetic license')
            self.assertFalse((tree / '.memory-bank').exists())
            objects = git(tree, 'rev-list', '--objects', '--missing=print', 'FETCH_HEAD').splitlines()
            self.assertIn('?' + excluded, objects)  # still absent in the object database, not just the worktree

    def test_standalone_install_does_not_adopt_unowned_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'keep'
            p.mkdir()
            result = subprocess.run([sys.executable, '-I', ROOT / 'install.py', '--prefix', p, '--wiki'],
                                    capture_output=True, text=True, env={'HOME': tmp, 'PATH': os.environ['PATH']})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not a verified Workbench installation', result.stderr)
            self.assertEqual(list(p.iterdir()), [])
