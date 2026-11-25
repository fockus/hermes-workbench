"""The default (including TUI) must ship both native plugins, never a shared Wiki."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Native(unittest.TestCase):
    def test_lock_pins_packaged_adapter_and_bank_scope_patch(self):
        import hashlib
        lock = json.loads((ROOT / 'workbench.lock.json').read_text())
        assets = lock['assets']
        for path in ('plugins/llm-wiki/__init__.py', 'plugins/llm-wiki/plugin.yaml',
                     'patches/memory-bank-profile.patch'):
            self.assertIn(path, assets)
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), assets[path])

    def test_wiki_rejects_missing_binding_and_cross_root_arguments(self):
        source = ROOT / 'plugins/llm-wiki/__init__.py'
        self.assertTrue(source.is_file(), 'missing native Wiki')
        spec = importlib.util.spec_from_file_location('wbwiki', source)
        assert spec and spec.loader
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        from unittest.mock import patch
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(plugin.WikiUnavailable):
            plugin._scope()
        for name, args in [('search', {'query': 'test', 'root': '/other'}),
                           ('read', {'path': 'wiki/../../other'}), ('read', {'path': '/outside'})]:
            with self.subTest(name=name, args=args), self.assertRaises(plugin.WikiUnavailable):
                plugin._arguments(name, args)
