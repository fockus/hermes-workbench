"""Exercise real Hermes hooks and tool dispatch; no model or provider credentials."""
import json
from pathlib import Path
import subprocess
import sys
import threading


def probe(root, *flags, cwd=None):
    env = dict(HOME=str(root / 'home'), HERMES_HOME=str(root / 'profile'), TMPDIR=str(root / 'tmp'),
               PATH=f'{root}/venv/bin:{root}/tool-bin:/usr/bin:/bin', PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([root / 'venv/bin/python', '-I', '-B', __file__, '--dispatch', root, *flags],
                   cwd=cwd or root / 'home', env=env, check=True, timeout=120)


def dispatch(root, marker=None, expected=None):
    from agent.skill_commands import get_skill_commands
    from hermes_cli.plugins import invoke_hook, render_system_prompt_sections
    from hermes_cli.plugins_session_env import resolve_session_env
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions, handle_function_call

    sid = 'workbench-acceptance'
    profile = root / 'profile'
    bank = profile / '.memory-bank'
    wiki = profile / 'llm-wiki'
    assert Path(get_skill_commands()['/mb']['skill_md_path']).resolve() == root / 'memory-bank/commands/mb.md'
    parts = {s.plugin: s.content for s in render_system_prompt_sections(dict(session_id=sid, cwd=str(Path.cwd())))}
    assert str(bank) in parts['memory-bank'] and '[MEMORY BANK: ACTIVE]' in parts['memory-bank']
    assert str(wiki) in parts['llm-wiki']
    env = resolve_session_env(sid)
    assert env['MB_PATH'] == str(bank) and env['MB_SKILLS_ROOT'] == str(root / 'memory-bank')

    enabled = sorted(_get_platform_tools(load_config(), 'cli'))
    catalog = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True, skip_tool_search_assembly=True)
    assert {'llm_wiki_' + n for n in ('status', 'search', 'read', 'graph')} <= {
        s['function']['name'] for s in catalog}

    def call(name, **args):
        return json.loads(handle_function_call('llm_wiki_' + name, args, session_id=sid, enabled_toolsets=enabled))

    def result(name, **args):
        reply = call(name, **args)
        assert reply.get('ok') and reply['scope'] == 'profile', (name, reply)
        return reply['result']

    status = result('status')
    assert status['indexed'] and not status['stale']
    assert result('graph', node='file:wiki/index.md')['nodes']
    assert result('read', path='wiki/index.md', start=1, end=1)['text']
    assert not call('read', path='wiki/../../.memory-bank/status.md').get('ok')
    hits = result('search', query=marker or 'WorkbenchMissingFixture', hops=0)['hits']
    if expected == 'present':
        assert marker and marker in parts['memory-bank'] and any(hit['path'] == 'raw/synthetic.md' for hit in hits)
        assert marker in result('read', path='raw/synthetic.md')['text']
        assert result('graph', node='file:raw/synthetic.md')['edges']
        invoke_hook('on_session_finalize', session_id=sid)
        assert sid in (bank / 'handoff/latest.md').read_text()
    elif expected == 'absent':
        assert marker and marker not in parts['memory-bank'] and not hits
        assert not call('read', path='raw/synthetic.md').get('ok')
        assert not (bank / 'handoff/latest.md').exists()
    for thread in threading.enumerate():
        if thread.name == 'memory-bank-soul-sync':
            thread.join(timeout=20)
    # Only the disposable Workbench profile is ever passed here.
    assert '<!-- mb-rules:start -->' in (profile / 'SOUL.md').read_text()
    print('PASS: native /mb, prompt, session env, Wiki status/search/read/graph;', expected or 'fresh profile')


if __name__ == '__main__':
    if sys.argv[1] == '--dispatch':
        dispatch(Path(sys.argv[2]).resolve(), *sys.argv[3:])
    else:
        probe(Path(sys.argv[1]).resolve())
