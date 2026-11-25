"""Operator-only Hermes commands. No model tool, session handoff or inherited login."""

import argparse
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time

VERSIONS = {
    'codex': 'codex-cli 0.170.0',
    'pi': '1.5.0',
    'opencode': 'opencode v2.0.18',
    'claude': '2.1.327 (Claude Code)',
}
ROUTE = re.compile(
    r'([A-Za-z0-9_-]{1,64})@127\.0\.0\.1:([1-9][0-9]{0,4})/([A-Za-z0-9][A-Za-z0-9._-]{0,95})\Z'
)
USAGE = 'usage: executor probe NAME | executor send NAME RECIPIENT@127.0.0.1:PORT/MODEL TEXT'
CAP = 256 * 1024


class Unavailable(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise Unavailable(reason)


def bindings(root, name):
    require(
        sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
        'macOS sandbox-exec required; no unsandboxed fallback',
    )
    bins = root / 'executors/bin'
    require(bins.resolve() == bins and (root / 'tmp').resolve() == root / 'tmp', 'unsafe Workbench directory')
    exe = (bins / name).resolve()
    require(
        exe.is_file() and os.access(exe, os.X_OK),
        f'{name} not configured: bind executors/bin/{name} to a trusted executable',
    )
    # Pi is also distributed as a Node package. Grant read access ONLY to that
    # package tree, not the Node installation, npm prefix, or a user's HOME.
    node, pkg = exe, exe
    if name == 'pi' and exe.name == 'cli.js':
        pkg = exe.parents[2]
        require(
            pkg.name == 'pi-coding-agent' and exe.relative_to(pkg).as_posix() == 'dist/bundle/cli.js',
            'unknown Pi package layout',
        )
        node = (bins / 'node').resolve()
        require(
            node.is_file() and os.access(node, os.X_OK),
            'Pi JS requires an explicit executors/bin/node binding',
        )
    return exe, node, pkg


def sandbox(bound, tmp, port=None, listen=None):
    exe, node, pkg = bound
    # Only reviewed binary/runtime data, disposable HOME and cwd. Keep the
    # vendor sandbox and approvals intact. Do not allow spawning shells/helpers.
    policy = """(version 1)(allow default)
    (deny file-read-data (require-all (vnode-type REGULAR-FILE) (require-not (require-any
      (subpath (param "TMP")) (literal (param "EXE")) (literal (param "NODE")) (subpath (param "PKG"))
      (subpath "/System/Library") (subpath "/usr/lib") (subpath "/usr/share") (subpath "/usr/icu")
      (subpath "/System/Volumes/Preboot/Cryptexes/OS/usr") (subpath "/private/var/db/timezone")))))
    (deny file-write* (require-not (require-any (subpath (param "TMP")) (literal "/dev/null"))))
    (deny file-test-existence file-read* (require-all (regex #"/[.](git|hg)(/|$)") (require-not (subpath (param "TMP")))))
    (deny process-exec* (require-not (require-any (literal (param "EXE")) (literal (param "NODE")))))
    (deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.securityd.xpc"))
    (deny network*)"""
    if port is not None:
        require(type(port) is int and 1024 <= port <= 65535, 'invalid route port')
        policy += f'(allow network-outbound (remote ip "localhost:{port}"))'
    if listen is not None:
        require(type(listen) is int and 1024 <= listen <= 65535, 'invalid private listener port')
        policy += f'(allow network-bind network-inbound (local ip "localhost:{listen}"))'
    return [
        '/usr/bin/sandbox-exec',
        '-D',
        'EXE=' + str(exe),
        '-D',
        'NODE=' + str(node),
        '-D',
        'PKG=' + str(pkg),
        '-D',
        'TMP=' + str(tmp),
        '-p',
        policy,
        *([str(node)] if node != exe else []),
        str(exe),
    ]


def capture(argv, env, cwd, data, timeout):
    # Regular input file avoids pipe backpressure deadlocks; child cannot inherit
    # the operator's terminal. Kill the whole process group even on interruption.
    with tempfile.TemporaryFile(dir=cwd) as inp:
        inp.write(data.encode())
        inp.seek(0)
        p = subprocess.Popen(
            argv,
            stdin=inp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        assert p.stdout and p.stderr
        deadline = time.monotonic() + timeout
        out, err = bytearray(), bytearray()
        try:
            with selectors.DefaultSelector() as sel:
                for fd, buf in [(p.stdout, out), (p.stderr, err)]:
                    sel.register(fd, selectors.EVENT_READ, buf)
                while sel.get_map():
                    left = deadline - time.monotonic()
                    require(left > 0, 'timeout')
                    for key, _ in sel.select(left):
                        block = os.read(key.fd, 8192)
                        if not block:
                            sel.unregister(key.fd)
                        else:
                            key.data.extend(block)
                            require(len(out) + len(err) <= CAP, 'output limit')
            try:
                rc = p.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise Unavailable('timeout') from None
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            p.stdout.close()
            p.stderr.close()

    def plain(buf):
        return ''.join(c for c in buf.decode(errors='replace') if c.isprintable() or c in '\n\t').strip()

    require(rc == 0, f'exit {rc}: {plain(err or out)[-1000:]}')
    return plain(out)


@contextmanager
def opencode_server(bound, tmp, env, port, timeout):
    # V2's --standalone chooses a random HTTP port internally. Never grant
    # outbound access to *all* localhost ports to make that work. Start the
    # same stdio-leased native server, with one explicitly confined port.
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        local = reserve.getsockname()[1]
    require(local != port, 'private port conflict; retry')
    url = f'http://127.0.0.1:{local}'
    env['OPENCODE_PASSWORD'] = secrets.token_urlsafe(32)  # per-invocation lease, not a user credential
    server = subprocess.Popen(
        [
            *sandbox(bound, tmp, port, local),
            'serve',
            '--stdio',
            '--hostname',
            '127.0.0.1',
            '--port',
            str(local),
        ],
        env=env,
        cwd=tmp / 'cwd',
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert server.stdin and server.stdout
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(server.stdout, selectors.EVENT_READ)
            require(sel.select(min(timeout, 10)), 'OpenCode private server readiness timeout')
            ready = os.read(server.stdout.fileno(), 1024)
        require(
            ready.strip() and json.loads(ready) == {'url': url} and server.poll() is None,
            'OpenCode private server failed readiness',
        )
        yield local
    finally:
        server.stdin.close()
        try:
            os.killpg(server.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        server.wait()
        server.stdout.close()


def request(name, tmp, env, port, model, text):
    base = f'http://127.0.0.1:{port}'
    # This public placeholder only selects a local compatible provider. It is
    # not a credential. The operator owns the endpoint and its upstream auth.
    marker = 'workbench-local-no-credential'
    if name == 'claude':
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_API_KEY=marker)
        return [
            '--bare',
            '-p',
            '--model',
            model,
            '--tools',
            '',
            '--strict-mcp-config',
            '--disable-slash-commands',
            '--no-session-persistence',
            '--permission-mode',
            'default',
        ], text
    if name == 'pi':
        agent = tmp / 'pi'
        agent.mkdir()
        env['PI_CODING_AGENT_DIR'] = str(agent)
        (agent / 'models.json').write_text(
            json.dumps(
                {
                    'providers': {
                        'workbench': {
                            'baseUrl': base + '/v1',
                            'api': 'openai-completions',
                            'apiKey': marker,
                            'models': [{'id': model}],
                        }
                    }
                }
            )
        )
        return [
            '-p',
            '--offline',
            '--no-session',
            '--no-tools',
            '--no-extensions',
            '--no-mcp',
            '--no-skills',
            '--no-context-files',
            '--no-prompt-templates',
            '--no-themes',
            '--no-approve',
            '--provider',
            'workbench',
            '--model',
            model,
        ], text
    if name == 'opencode':
        cfg = {
            'providers': {
                'workbench': {
                    'name': 'Workbench',
                    'package': '@opencode/ai/providers/openai-compatible',
                    'settings': {'baseURL': base + '/v1', 'apiKey': marker},
                    'models': {model: {'name': model}},
                }
            },
            'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}],
        }
        (tmp / 'cwd/opencode.json').write_text(json.dumps(cfg))
        return ['--model', 'workbench/' + model, 'Review the literal task supplied on stdin.'], text
    return [
        'exec',
        '--ephemeral',
        '--sandbox',
        'read-only',
        '--color',
        'never',
        '--disable',
        'shell_tool',
        '--disable',
        'multi_agent',
        '-c',
        'web_search="disabled"',
        '-c',
        'model_provider="workbench"',
        '-c',
        'model_providers.workbench.name="Workbench"',
        '-c',
        f'model_providers.workbench.base_url="{base}/v1"',
        '-c',
        'model_providers.workbench.wire_api="responses"',
        '--model',
        model,
        '-',
    ], text


def run(root, raw, timeout, confirm):
    fields = raw.split(None, 3)
    require(len(fields) >= 2 and fields[0] in ('probe', 'send'), USAGE)
    op, name = fields[:2]
    require(name in VERSIONS, 'unknown executor: use ' + ', '.join(VERSIONS))
    require(len(fields) == (2 if op == 'probe' else 4), USAGE)
    port = model = None
    if op == 'send':
        routes = root / 'executors/routes.json'
        require(
            routes.is_file() and routes.resolve() == routes and routes.stat().st_size < 8192,
            'missing or unsafe routes.json',
        )
        config = json.loads(routes.read_text())
        require(isinstance(config, dict), 'invalid routes.json; expected an object')
        configured = config.get(name)
        require(
            isinstance(configured, str) and configured == fields[2],
            'consent does not match the configured recipient, port and model',
        )
        match = ROUTE.fullmatch(configured)
        require(
            match and 1024 <= int(match[2]) <= 65535,
            'invalid route; only a named loopback destination is supported',
        )
        assert match
        port, model = int(match[2]), match[3]
        require(
            0 < len(fields[3].encode()) <= 16384 and '\0' not in fields[3],
            'task must be 1..16384 bytes of literal text',
        )
    bound = bindings(root, name)
    if op == 'send':
        question = (
            f'Send this exact text via {name} to {fields[2]}? The endpoint may forward it to a paid '
            f'provider. No project files are attached.\n\n{fields[3]!r}'
        )
        require(confirm is not None and confirm(question) is True, 'not confirmed; nothing sent')
    with tempfile.TemporaryDirectory(dir=root / 'tmp', prefix='executor-') as t:
        tmp = Path(t)
        home = tmp / 'home'
        home.mkdir()
        cwd = tmp / 'cwd'
        cwd.mkdir()
        env = dict(
            HOME=str(home),
            TMPDIR=t,
            PATH='/usr/bin:/bin',
            LANG='C.UTF-8',
            OPENSSL_CONF='/dev/null',
            XDG_CONFIG_HOME=str(home / 'config'),
            XDG_DATA_HOME=str(home / 'data'),
            XDG_STATE_HOME=str(home / 'state'),
            XDG_CACHE_HOME=str(home / 'cache'),
            PI_OFFLINE='1',
            PI_TELEMETRY='0',
            OPENCODE_DISABLE_MODELS_FETCH='true',
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
            DISABLE_TELEMETRY='1',
            NO_COLOR='1',
        )
        probe = sandbox(bound, tmp)
        version = capture([*probe, '--version'], env, cwd, '', min(10, timeout))
        require(
            version == VERSIONS[name], f'{name}: unsupported version {version!r}; expected {VERSIONS[name]}'
        )
        if op == 'probe':
            flags = (
                ['exec', '--help']
                if name == 'codex'
                else ['run', '--help']
                if name == 'opencode'
                else ['--help']
            )
            capture([*probe, *flags], env, cwd, '', min(10, timeout))
            return version + ' (local version/help only; no auth or model check)'
        if name == 'codex':
            # Satisfy the native repository check; do not bypass trust with a flag.
            subprocess.run(
                ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', 'init', '--quiet', '--template=', '.'],
                env={**env, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull},
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=5,
            )
        args, data = request(name, tmp, env, port, model, fields[3])
        with (
            opencode_server(bound, tmp, env, port, timeout) if name == 'opencode' else nullcontext(port)
        ) as peer:
            if name == 'opencode':
                args = ['run', '--server', f'http://127.0.0.1:{peer}', *args]
            result = capture([*sandbox(bound, tmp, peer), *args], env, cwd, data, timeout)
        require(result, 'executor returned no task output')
        return result


def command(root, raw, timeout=60, *, confirm=None):
    try:
        return run(root, raw, timeout, confirm)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return 'Executor error: ' + str(exc)


def installed_root():
    root = Path(__file__).resolve().parents[3]
    require(
        Path(os.environ.get('HERMES_HOME', '')) == root / 'profile'
        and Path(os.environ.get('HOME', '')) == root / 'home',
        'use the isolated Workbench launcher',
    )
    return root


def slash(raw_args, *, platform=None, confirm=None):
    if platform not in ('cli', 'tui'):
        return 'Executor error: local Workbench CLI/TUI only'
    return command(installed_root(), raw_args, confirm=confirm)


def register(ctx):
    def session_command(raw_args, *, platform=None):
        return slash(
            raw_args,
            platform=platform,
            confirm=lambda question: ctx.ask_choice(question, ['Cancel', 'Send once']) == 'Send once',
        )

    ctx.register_command(
        'executor',
        handler=session_command,
        description='Send literal text to an explicitly selected coding CLI',
        args_hint='probe NAME | send NAME RECIPIENT TEXT',
    )

    def setup(parser):
        parser.add_argument('request', nargs=argparse.REMAINDER, help=USAGE)

    def cli(args):
        def confirm(question):
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                return False
            print(
                question + '\nType SEND to confirm, anything else to cancel: ',
                file=sys.stderr,
                end='',
                flush=True,
            )
            return sys.stdin.readline().rstrip('\r\n') == 'SEND'

        result = slash(' '.join(args.request), platform='cli', confirm=confirm)
        print(result)
        if result.startswith('Executor error:'):
            raise SystemExit(1)

    ctx.register_cli_command(
        name='executor', help='Run an explicitly configured coding CLI', setup_fn=setup, handler_fn=cli
    )
