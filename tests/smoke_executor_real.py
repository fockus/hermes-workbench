"""Offline vendor CLI integration. The local HTTP fixture is NOT an LLM or an auth test."""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re

from pathlib import Path
import subprocess
import sys
import threading
import uuid

NAMES = ('codex', 'pi', 'opencode', 'claude')


@contextmanager
def loopback(token):
    calls = []
    reply = 'FIXTURE-REPLY-' + uuid.uuid4().hex

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, token in json.dumps(body)))
            model = body.get('model', 'demo')

            def event(kind, obj):
                return 'event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **obj}) + '\n\n'

            if self.path.startswith('/v1/messages'):
                stream = ''.join(
                    [
                        event(
                            'message_start',
                            {
                                'message': {
                                    'id': 'msg_test',
                                    'type': 'message',
                                    'role': 'assistant',
                                    'model': model,
                                    'content': [],
                                    'stop_reason': None,
                                    'usage': {'input_tokens': 1, 'output_tokens': 0},
                                }
                            },
                        ),
                        event(
                            'content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}}
                        ),
                        event(
                            'content_block_delta',
                            {'index': 0, 'delta': {'type': 'text_delta', 'text': reply}},
                        ),
                        event('content_block_stop', {'index': 0}),
                        event(
                            'message_delta',
                            {
                                'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                'usage': {'output_tokens': 1},
                            },
                        ),
                        event('message_stop', {}),
                    ]
                )
            elif self.path.startswith('/v1/responses'):
                item = {
                    'id': 'msg_test',
                    'type': 'message',
                    'role': 'assistant',
                    'status': 'completed',
                    'content': [{'type': 'output_text', 'text': reply, 'annotations': []}],
                }
                resp = {
                    'id': 'resp_test',
                    'object': 'response',
                    'model': model,
                    'output': [item],
                    'status': 'completed',
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
                }
                stream = ''.join(
                    [
                        event('response.created', {'response': dict(resp, status='in_progress', output=[])}),
                        event(
                            'response.output_item.added',
                            {'output_index': 0, 'item': dict(item, status='in_progress', content=[])},
                        ),
                        event(
                            'response.content_part.added',
                            {
                                'item_id': 'msg_test',
                                'output_index': 0,
                                'content_index': 0,
                                'part': {'type': 'output_text', 'text': '', 'annotations': []},
                            },
                        ),
                        event(
                            'response.output_text.delta',
                            {'item_id': 'msg_test', 'output_index': 0, 'content_index': 0, 'delta': reply},
                        ),
                        event('response.output_item.done', {'output_index': 0, 'item': item}),
                        event('response.completed', {'response': resp}),
                    ]
                )
            elif self.path.startswith('/v1/chat/completions'):

                def chunk(delta, why):
                    return (
                        'data: '
                        + json.dumps(
                            {
                                'id': 'chatcmpl-test',
                                'object': 'chat.completion.chunk',
                                'created': 1,
                                'model': model,
                                'choices': [{'index': 0, 'delta': delta, 'finish_reason': why}],
                            }
                        )
                        + '\n\n'
                    )

                stream = (
                    chunk({'role': 'assistant', 'content': reply}, None)
                    + chunk({}, 'stop')
                    + 'data: [DONE]\n\n'
                )
            else:
                self.send_error(404)
                return
            data = stream.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, reply, calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def smoke(root, binaries, node, invoke):
    # Refuse to overwrite any operator bindings and remove only our own links/config.
    binds = root / 'executors/bin'
    routes = root / 'executors/routes.json'
    assert not routes.exists() and not routes.is_symlink() and not list(binds.iterdir()), (
        'requires a fresh, unconfigured prefix'
    )
    links = []
    try:
        for name, path in {**binaries, 'node': node}.items():
            link = binds / name
            link.symlink_to(Path(path).resolve(strict=True))
            links.append(link)
        for name in binaries:
            version = invoke('probe ' + name)
            assert 'local version/help only' in version, (name, version)
            token = 'SYNTHETIC-TASK-' + uuid.uuid4().hex
            with loopback(token) as (port, reply, calls):
                recipient = f'synthetic@127.0.0.1:{port}/demo'
                with routes.open('x') as f:
                    json.dump({name: recipient}, f)
                try:
                    result = invoke(
                        f'send {name} {recipient} Review this literal marker: {token}',
                        fixture=(name, str(port), token),
                    )
                    assert reply in result and any(ok for _, ok in calls), (name, result, calls)
                    print(
                        'PASS real CLI → local protocol fixture → Hermes:',
                        name,
                        version,
                        [u for u, ok in calls if ok],
                        flush=True,
                    )
                finally:
                    routes.unlink()
    finally:
        for link in links:
            link.unlink()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--native-slash-dispatch':
        if len(sys.argv) != 6:
            raise SystemExit('missing smoke fixture')
        # Pin consent to the live loopback fixture, not to values parsed from raw.
        raw, name, port, token = sys.argv[2:]
        if (
            name not in NAMES
            or not re.fullmatch(r'[1-9][0-9]{3,4}', port)
            or not 1024 <= int(port) <= 65535
            or not re.fullmatch(r'SYNTHETIC-TASK-[0-9a-f]{32}', token)
        ):
            raise SystemExit('invalid smoke fixture')
        route = f'synthetic@127.0.0.1:{port}/demo'
        data = f'Review this literal marker: {token}'
        if raw != f'send {name} {route} {data}':
            raise SystemExit('command does not match smoke fixture')
        expected_question = (
            f'Send this exact text via {name} to {route}? The endpoint may forward it to a paid '
            f'provider. No project files are attached.\n\n{data!r}'
        )

        from hermes_cli.plugins import (
            discover_plugins,
            get_plugin_command_handler,
            resolve_plugin_command_result,
        )
        from hermes_cli.plugin_command_dispatch import invoke_plugin_command
        from hermes_cli.plugin_choices import bind_asker

        discover_plugins()
        handler = get_plugin_command_handler('executor')
        if not handler:
            raise SystemExit('missing executor handler')

        def synthetic_choice(question, options):
            if question == expected_question and options == ['Cancel', 'Send once']:
                return 'Send once'
            return 'Cancel'

        with bind_asker(synthetic_choice):
            print(
                resolve_plugin_command_result(
                    invoke_plugin_command(handler, raw, platform='tui', source=None)
                )
            )
        raise SystemExit(0)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('prefix', type=Path)
    p.add_argument('--node', required=True)

    for n in NAMES:
        p.add_argument('--' + n, required=True)
    a = p.parse_args()
    root = a.prefix.resolve()

    def invoke(raw, *, fixture=()):
        env = dict(
            HOME=str(root / 'home'),
            HERMES_HOME=str(root / 'profile'),
            TMPDIR=str(root / 'tmp'),
            PATH=f'{root}/venv/bin:{root}/tool-bin:/usr/bin:/bin',
            PYTHONDONTWRITEBYTECODE='1',
        )
        argv = [root / 'workbench', 'executor', *raw.split(None, 3)]
        if raw.startswith('send '):
            refused = subprocess.run(
                argv,
                env=env,
                cwd=root / 'home',
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=40,
            )
            assert refused.returncode != 0 and 'not confirmed' in refused.stdout, refused
            argv = [
                root / 'venv/bin/python',
                '-I',
                '-B',
                Path(__file__).resolve(),
                '--native-slash-dispatch',
                raw,
                *fixture,
            ]
        cp = subprocess.run(argv, env=env, cwd=root / 'home', capture_output=True, text=True, timeout=90)
        assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
        return cp.stdout.strip()

    smoke(root, {n: getattr(a, n) for n in NAMES}, a.node, invoke)
