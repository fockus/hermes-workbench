"""Process fixtures, not vendor agents. No model provider, credentials or caller PATH."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'plugins/workbench-executors/__init__.py'
VERSIONS = {
    'codex': 'codex-cli 0.170.0',
    'pi': '1.5.0',
    'opencode': 'opencode v2.0.18',
    'claude': '2.1.327 (Claude Code)',
}
TARGET = 'test@127.0.0.1:18443/demo'
# Native executables so the fixture cannot depend on permission to run a shell or Python.
C = r"""#include <stdio.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int n, char **v) {
 if (n==2 && !strcmp(v[1],"--version")) {puts(VERSION);return 0;}
 if (n>2 && !strcmp(v[1],"serve")) {printf("{\"url\":\"http://127.0.0.1:%s\"}\n",v[n-1]);fflush(stdout);while(getchar()!=EOF){} return 0;}
 char input[32768]={0}, cwd[4096]; fread(input,1,sizeof(input)-1,stdin);
 const char *q = input[0] ? input : v[n-1];
 if(strstr(q,"TIMEOUT")){sleep(8);return 0;}
 if(strstr(q,"ERROR")){fputs("synthetic failure",stderr);return 7;}
 if(strstr(q,"FLOOD")){for(int i=0;i<1000000;i++)putchar('x');return 0;}
 if(strstr(q,"CANARY ")){int s=socket(AF_INET,SOCK_STREAM,0);struct sockaddr_in a={0};a.sin_family=AF_INET;
 a.sin_port=htons(atoi(strstr(q,"CANARY ")+7));a.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
 puts(connect(s,(void*)&a,sizeof(a))==0?"WRONG PEER REACHABLE":"wrong peer blocked");return 0;}
 if(getenv("WB_PRIVATE_MARKER")||getenv("HERMES_HOME"))return 13;
 if(fopen(FOREIGN,"r") || fopen(FOREIGN,"a"))return 14;
 printf("Fixture cwd=%s\\n",getcwd(cwd,sizeof(cwd)));for(int i=1;i<n;i++)printf("[%s]",v[i]);
 printf(" data=%s",q);return 0;
}"""


def setup_fixtures(root):
    (root / 'tmp').mkdir(exist_ok=True)
    bins = root / 'executors/bin'
    bins.mkdir(parents=True, exist_ok=True)
    foreign = root / 'not-approved.txt'
    foreign.write_text('synthetic')
    for name, version in VERSIONS.items():
        subprocess.run(
            [
                'cc',
                '-x',
                'c',
                '-',
                '-o',
                bins / name,
                '-DVERSION=' + json.dumps(version),
                '-DFOREIGN=' + json.dumps(str(foreign)),
            ],
            input=C,
            text=True,
            check=True,
        )
    (root / 'executors/routes.json').write_text(json.dumps(dict.fromkeys(VERSIONS, TARGET)))


class Executors(unittest.TestCase):
    def test_install_opt_in(self):
        r = subprocess.run(
            [sys.executable, '-I', ROOT / 'install.py', '--help'], capture_output=True, text=True
        )
        self.assertIn('--with-executors', r.stdout)

    def setUp(self):
        if self._testMethodName == 'test_install_opt_in':
            return
        self.assertTrue(SOURCE.is_file(), 'missing native executor plugin')
        spec = importlib.util.spec_from_file_location('wbexec', SOURCE)
        assert spec and spec.loader
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        if sys.platform != 'darwin' or not shutil.which('cc'):
            self.skipTest('process policy and native fixtures require macOS and cc')
        setup_fixtures(self.root)

    def call(self, raw, **kw):
        return self.api.command(self.root, raw, confirm=lambda _: True, **kw)

    def test_four_processes_literal_data_cwd_and_flags(self):
        data = 'Review: $(touch ignored); @file --help & "quoted"'
        flags = {'pi': '--no-tools', 'claude': '--bare', 'opencode': '--server', 'codex': 'read-only'}
        with patch.dict(os.environ, {'WB_PRIVATE_MARKER': 'do not pass'}):
            for name in VERSIONS:
                with self.subTest(name=name):
                    r = self.call(f'send {name} {TARGET} {data}')
                    self.assertIn('Fixture cwd=' + str(self.root / 'tmp') + '/', r)
                    self.assertIn(flags[name], r)
                    self.assertIn(data, r)
                    self.assertIn(VERSIONS[name], self.call('probe ' + name))
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])

    def test_rejections_without_fallback(self):
        for raw, error in [
            ('send pi elsewhere text', 'consent'),
            ('send pi', 'usage'),
            ('probe bash', 'unknown'),
        ]:
            with self.subTest(raw=raw):
                self.assertIn(error, self.call(raw).lower())
        (self.root / 'executors/bin/pi').unlink()
        self.assertIn('not configured', self.call('probe pi'))
        (self.root / 'executors/routes.json').write_text(json.dumps({'codex': 'secret@https://outside/demo'}))
        self.assertIn('route', self.call('send codex secret@https://outside/demo text'))

    def test_failure_timeout_output_cap(self):
        for marker, expect, timeout in [
            ('TIMEOUT', 'timeout', 0.25),
            ('ERROR', 'exit 7', 3),
            ('FLOOD', 'output limit', 3),
        ]:
            with self.subTest(marker=marker):
                self.assertIn(expect, self.call(f'send pi {TARGET} {marker}', timeout=timeout))

    def test_gateway_refused(self):
        self.assertIn('local', self.api.slash('probe pi', platform='telegram').lower())
        with patch.object(self.api, 'installed_root', return_value=self.root):
            self.assertIn('local version/help only', self.api.slash('probe pi', platform='tui'))

    def test_network_limited_to_consented_peer(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            self.assertIn(
                'wrong peer blocked', self.call(f'send codex {TARGET} CANARY {server.getsockname()[1]}')
            )

    def test_wrong_version_does_not_receive_task(self):
        bins = self.root / 'executors/bin'
        shutil.copyfile(bins / 'codex', bins / 'claude')
        self.assertIn('unsupported version', self.call(f'send claude {TARGET} data'))

    def test_no_confirmation_no_dispatch(self):
        with patch.object(self.api, 'installed_root', return_value=self.root):
            result = self.api.slash(f'send pi {TARGET} synthetic', platform='tui')
            self.assertIn('not confirmed', result)
