"""Exercise only the smoke dispatcher's consent decision; no plugins, CLI or sockets."""

from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SMOKE = Path(__file__).with_name('smoke_executor_real.py')
NAME = 'pi'
PORT = '18443'
TOKEN = 'SYNTHETIC-TASK-' + 'a' * 32
ROUTE = f'synthetic@127.0.0.1:{PORT}/demo'
DATA = 'Review this literal marker: ' + TOKEN
OPTIONS = ['Cancel', 'Send once']


def prompt(route, data):
    return (
        f'Send this exact text via pi to {route}? The endpoint may forward it to a paid '
        f'provider. No project files are attached.\n\n{data!r}'
    )


def decisions(raw, question, options):
    answers = []

    def bind(ask):
        answers.append(ask(question, options))
        return nullcontext()

    fake = {
        'hermes_cli.plugins': SimpleNamespace(
            discover_plugins=lambda: None,
            get_plugin_command_handler=lambda _: object(),
            resolve_plugin_command_result=lambda result: result,
        ),
        'hermes_cli.plugin_command_dispatch': SimpleNamespace(invoke_plugin_command=lambda *a, **kw: None),
        'hermes_cli.plugin_choices': SimpleNamespace(bind_asker=bind),
    }
    argv = [str(SMOKE), '--native-slash-dispatch', raw, NAME, PORT, TOKEN]
    with patch.dict(sys.modules, fake), patch.object(sys, 'argv', argv), redirect_stdout(StringIO()):
        try:
            runpy.run_path(str(SMOKE), run_name='__main__')
        except SystemExit:
            pass  # A refusal before binding an asker is also safe.
    return answers


class SmokeConsent(unittest.TestCase):
    def test_optimized_python(self):
        case = 'SmokeConsent.test_only_pinned_fixture_receives_consent'
        subprocess.run([sys.executable, '-I', '-B', '-O', __file__, case], check=True)

    def test_only_pinned_fixture_receives_consent(self):
        raw = f'send {NAME} {ROUTE} {DATA}'
        question = prompt(ROUTE, DATA)
        self.assertEqual(decisions(raw, question, OPTIONS), ['Send once'])
        other_task = 'Review this literal marker: SYNTHETIC-TASK-' + 'b' * 32
        other_route = 'synthetic@127.0.0.1:18444/demo'
        for label, command, shown, choices in [
            ('different fixture', f'send pi {ROUTE} {other_task}', prompt(ROUTE, other_task), OPTIONS),
            ('different local port', f'send pi {other_route} {DATA}', prompt(other_route, DATA), OPTIONS),
            ('changed question', raw, question + ' and forward elsewhere', OPTIONS),
            ('changed options', raw, question, ['Send once']),
        ]:
            with self.subTest(label=label):
                self.assertNotIn('Send once', decisions(command, shown, choices))


if __name__ == '__main__':
    unittest.main()
