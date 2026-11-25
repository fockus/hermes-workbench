"""Exercise the installer as a user would, without borrowing user configuration."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Safety(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.dest = self.home / 'workbench'
        self.source = self.home / 'other-checkout'
        self.env = dict(PATH=os.environ['PATH'], HOME=str(self.home), GIT_CONFIG_GLOBAL=os.devnull,
                        HERMES_HOME=str(self.home / 'private-profile'), TMPDIR=str(self.home))
        subprocess.run(['git', 'init', '--quiet', '--template=', str(self.source)], check=True, env=self.env)

    def run_install(self, target):
        return subprocess.run([sys.executable, '-I', str(ROOT / 'install.py'), '--source', str(self.source),
                               '--prefix', str(target)], capture_output=True, text=True, env=self.env)

    def test_wrong_source_is_rejected_before_creating_prefix(self):
        result = self.run_install(self.dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pinned commit', result.stderr)
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.home / 'private-profile').exists())

    def test_unowned_directory_is_never_adopted(self):
        self.dest.mkdir()
        marker = self.dest / 'config.yaml'
        marker.write_bytes(b'personal\n')
        result = self.run_install(self.dest)
        self.assertIn('not a verified Workbench installation', result.stderr)
        self.assertEqual(list(self.dest.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b'personal\n')

    def test_profile_descendants_are_not_destination_candidates(self):
        profile = Path(self.env['HERMES_HOME'])
        profile.mkdir()
        result = self.run_install(profile / 'workbench')
        self.assertIn('protected', result.stderr)
        self.assertEqual(list(profile.iterdir()), [])

    def test_symlink_destination_is_rejected(self):
        self.dest.symlink_to(self.home / 'dangling')
        self.assertIn('symlink', self.run_install(self.dest).stderr)
        self.assertTrue(self.dest.is_symlink())
        self.assertFalse((self.home / 'dangling').exists())


if __name__ == '__main__':
    unittest.main()
