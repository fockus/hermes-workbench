#!/usr/bin/env python3
"""Install a private, pinned Hermes source distribution, never into an existing checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
LOCK = json.loads((ROOT / 'workbench.lock.json').read_text())
PATCH = ROOT / 'patches/status-rule.patch'
SKIN = 'skins/claude-graphite.yaml'
BUNDLE = 'tui/dist/entry.js'
RECEIPT = '.workbench-installed.json'
EXEC_LOCK = ROOT / 'executors.lock.json'
FULL_RECEIPT = '.workbench-full-profile.json'
PENDING = '.workbench-full-pending'
# Conservative unattended policy for these exact locked complete source trees, not a grant.
PROFILE_HOLDBACK = {n: 'native community scan: caution; not admitted unattended'
                    for n in ('omh', 'soul-composer', 'workflows')}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args, env, cwd, *, capture=False):
    result = subprocess.run(args, env=env, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def environment(home, tmp):
    return dict(HOME=str(home), HERMES_HOME=str(home / 'profile'), TMPDIR=str(tmp),
                PATH=os.pathsep.join(p for p in os.get_exec_path() if os.path.isabs(p)),
                XDG_CACHE_HOME=str(home / 'cache'), XDG_CONFIG_HOME=str(home / 'config'),
                XDG_DATA_HOME=str(home / 'data'), XDG_STATE_HOME=str(home / 'state'),
                GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1',
                GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', CI='true', LEFTHOOK='0',
                PNPM_HOME=str(home / 'pnpm'), UV_CACHE_DIR=str(home / 'uv'),
                NPM_CONFIG_USERCONFIG=os.devnull, NPM_CONFIG_GLOBALCONFIG=os.devnull)


def target(raw, source):
    dest = Path(raw).expanduser().absolute()
    require('..' not in dest.parts and not dest.is_symlink(), 'symlink or ambiguous destination')
    dest = dest.parent.resolve(strict=True) / dest.name
    protected = [ROOT, Path.home() / '.hermes']
    if os.environ.get('HERMES_HOME'):
        protected.append(Path(os.environ['HERMES_HOME']))
    if source:
        protected.append(source)
    require(all(not dest.is_relative_to(p.resolve()) and not p.resolve().is_relative_to(dest)
                for p in protected), 'protected source, distribution or profile path')
    return dest


def ui_artifacts():
    return {'workbench': sha(ROOT / 'workbench'), 'profile/' + SKIN: LOCK['assets'][SKIN],
            'source/LICENSE': LOCK['assets']['LICENSES/Hermes-Agent.txt'],
            **{'source/' + p: h for p, h in LOCK['patched'].items()}}


def verify_existing(dest, mode, executors=False, *, full=False, preparing=False):
    receipt = dest / RECEIPT
    require(receipt.is_file() and not receipt.is_symlink(), 'not a verified Workbench installation')
    data = json.loads(receipt.read_text())
    require(data.get('lock') == sha(ROOT / 'workbench.lock.json') and data.get('prefix') == str(dest),
            'different Workbench lock or relocated installation; choose a new prefix')
    require(data.get('mode') == mode, 'different installation mode; choose a new prefix')
    require(data.get('full_profile', False) is full, 'different profile selection; choose a new prefix')
    if full and not preparing:
        require(not (dest/PENDING).exists(), 'incomplete full profile; choose a new prefix')
        final = dest/FULL_RECEIPT
        require(final == final.resolve() and final.is_file() and json.loads(final.read_text()) == full_record(dest),
                'missing or changed full-profile files; nothing overwritten')
    require(data.get('executors') == (sha(EXEC_LOCK) if executors else None),
            'different executor option or lock; choose a new prefix')
    require(export_digest(dest / 'llm-wiki-src') == LOCK['components']['llm-wiki']['digest'], 'drift: llm-wiki')
    expected = {'llm-wiki': sha(ROOT / 'llm-wiki')}
    if executors:
        expected.update({'profile/' + p: h for p, h in json.loads(EXEC_LOCK.read_text()).items()})
    expected.update({p: sha(ROOT / p) for p in legal_files(mode)})
    entries = ['llm-wiki', 'wiki-venv/bin/python']
    if mode != 'wiki':
        require(export_digest(dest / 'memory-bank') == LOCK['components']['memory-bank']['installed_digest'],
                'drift: memory-bank')
        expected['workbench'] = sha(ROOT / 'workbench')
        if not full:
            expected.update({'profile/' + p: h for p, h in LOCK['assets'].items() if p.startswith('plugins/')})
        entries.extend(['workbench', 'venv/bin/python'])
        expected['source/LICENSE'] = LOCK['assets']['LICENSES/Hermes-Agent.txt']
        entries.extend(['venv/bin/hermes', 'tool-bin/jq'])
        for link, obj in mb_links(dest, full).items():
            require(link.is_symlink() and link.resolve() == obj and obj.exists(), f'drift: {link}')
    if mode == 'tui':
        expected.update(ui_artifacts() | {BUNDLE: data.get('bundle')})
        entries.append('tool-bin/node')
    for path, digest in expected.items():
        obj = dest / path
        allowed = {digest}
        if path == 'workbench' and not full:
            # Same base lock, pre-pack launcher: do not require migrating an existing base profile.
            allowed.add('2122f7ad59dd77d9c970f4e719fbdfedf4ac3746aef91c2a56a9b83ef6081f04')
        require(obj.resolve() == obj and obj.is_file() and sha(obj) in allowed, f'drift: {path}; nothing overwritten')
    for entry in entries:
        require(os.access(dest / entry, os.X_OK), f'missing executable: {entry}')
    print('Verified Workbench runtime files; configuration untouched.')


def tool(name, expected, env, tmp):
    exe = shutil.which(name, path=env['PATH'])
    require(exe, f'{name} missing; expected {expected} on PATH')
    version = run([exe, '--version'], env, tmp, capture=True)
    require(version == expected or (name == 'uv' and version.startswith(expected + ' ')),
            f'{name}: expected {expected}, found {version}')
    return exe


def prepare(source, tmp, env, ui):
    git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'fetch.fsckObjects=true']
    pin = LOCK['commit']
    if source:
        try:
            head = run([*git, '-C', source, 'rev-parse', '--verify', 'HEAD^{commit}'], env, tmp, capture=True)
        except subprocess.CalledProcessError as e:
            raise ValueError('source must be a checkout of the pinned commit') from e
        require(head == pin, f'source must have pinned commit {pin} at HEAD')
        url, ref = source.as_uri(), pin
    else:
        url, ref = LOCK['upstream'], 'refs/tags/' + LOCK['tag']
        resolved = run([*git, 'ls-remote', '--exit-code', url, ref + '^{}'], env, tmp, capture=True)
        require(resolved.split() == [pin, ref + '^{}'], 'upstream tag no longer peels to the pinned commit')
    require(sys.version_info[:2] == tuple(map(int, LOCK['python'].split('.'))), 'use Python ' + LOCK['python'])
    bins = {'uv': tool('uv', 'uv ' + LOCK['uv'], env, tmp), 'jq': shutil.which('jq', path=env['PATH'])}
    require(bins['jq'], 'jq missing; required for Memory Bank')
    if ui:
        for name, expected in [('node', 'v' + LOCK['node']), ('pnpm', LOCK['pnpm'])]:
            bins[name] = tool(name, expected, env, tmp)
    tree = tmp / 'source'
    run([*git, 'init', '--quiet', '--template=', tree], env, tmp)
    run([*git, 'fetch', '--quiet', '--depth=1', '--no-tags', url, ref], env, tree)
    require(run([*git, 'rev-parse', 'FETCH_HEAD^{commit}'], env, tree, capture=True) == pin, 'fetched commit differs')
    run([*git, 'checkout', '--quiet', '--detach', pin], env, tree)
    require(sha(tree / 'LICENSE') == LOCK['assets']['LICENSES/Hermes-Agent.txt'], 'upstream license differs')
    if not ui:
        return tree, bins
    run([*git, 'apply', '--check', '--index', PATCH], env, tree)
    run([*git, 'apply', '--index', PATCH], env, tree)
    changed = run([*git, 'diff', '--cached', '--name-only', pin], env, tree, capture=True).splitlines()
    require(set(changed) == set(LOCK['patched']), 'unexpected patch scope')
    for path, digest in LOCK['patched'].items():
        require(sha(tree / path) == digest, 'unexpected patched bytes: ' + path)
    return tree, bins


def install_ui(dest, repo, bins, env):
    (dest / 'tool-bin/node').symlink_to(bins['node'])
    run([bins['pnpm'], 'install', '--filter', 'hermes-tui...', '--frozen-lockfile', '--trust-lockfile',
         '--ignore-scripts', '--prod=false', '--store-dir', str(Path(env['HOME']) / 'store'),
         '--config.package-import-method=copy'], env, repo)
    run([bins['pnpm'], '-C', 'ui-tui', 'run', 'build:ink'], env, repo)
    run([bins['pnpm'], '-C', 'ui-tui', 'exec', 'vitest', 'run',
         *[p.removeprefix('ui-tui/') for p in LOCK['patched'] if '/__tests__/' in p]], env, repo)
    run([bins['node'], 'scripts/build/tui.mjs', '--source', repo, '--out', dest / 'tui'], env, repo)
    run([bins['node'], '--check', dest / BUNDLE], env, repo)
    shutil.copyfile(ROOT / SKIN, dest / 'profile' / SKIN)


def copy_launcher(dest, name):
    launcher = dest / name
    shutil.copyfile(ROOT / name, launcher)
    launcher.chmod(0o700)
    return launcher


def legal_files(mode):
    third_party = ['LLM-Wiki'] if mode == 'wiki' else ['Hermes-Agent', 'Memory-Bank', 'Superpowers', 'LLM-Wiki']
    return ['LICENSE', 'NOTICE.md', *[f'LICENSES/{name}.txt' for name in third_party]]


def receipt_for(dest, mode, executors=False, *, full=False):
    for path in legal_files(mode):
        (dest / path).parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / path, dest / path)
    with (dest / RECEIPT).open('x') as receipt:
        json.dump(dict(mode=mode, lock=sha(ROOT / 'workbench.lock.json'),
                       **({'full_profile': True} if full else {}),
                       **({'executors': sha(EXEC_LOCK)} if executors else {}),
                       bundle=sha(dest / BUNDLE) if mode == 'tui' else None, prefix=str(dest)), receipt)


def export_digest(tree):
    require(tree.is_dir() and tree.resolve() == tree, f'unsafe component directory: {tree}')
    result = hashlib.sha256()
    for obj in sorted(tree.rglob('*')):
        require(not obj.is_symlink(), f'unexpected symlink: {obj}')
        if not obj.is_dir():
            require(obj.is_file(), f'not a regular file: {obj}')
            record = [obj.relative_to(tree).as_posix(), bool(obj.stat().st_mode & 0o111), sha(obj)]
            result.update(json.dumps(record).encode() + b'\n')
    return result.hexdigest()


def checkout_component(tree, name, spec, env):
    # Fetch Git metadata, then materialize blobs ONLY for the reviewed public runtime paths.
    git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'fetch.fsckObjects=true']
    run([*git, 'init', '--quiet', '--template=', tree], env, tree.parent)
    run([*git, 'remote', 'add', 'origin', spec['upstream']], env, tree)
    # Git silently downloads every blob when a server lacks filtering. Inspect its v2
    # capability advertisement with ls-remote (metadata only) BEFORE requesting any pack.
    trace = tree / '.git/capabilities.trace'
    run([*git, '-c', 'protocol.version=2', 'ls-remote', 'origin', 'HEAD'],
        {**env, 'GIT_TRACE_PACKET': str(trace)}, tree, capture=True)
    require(any('filter' in line.partition('< fetch=')[2].split() for line in trace.read_text().splitlines()),
            f'{name} origin did not advertise blob filtering; refusing an unfiltered fetch')
    trace.unlink()
    run([*git, 'config', 'remote.origin.promisor', 'true'], env, tree)
    run([*git, 'config', 'remote.origin.partialclonefilter', 'blob:none'], env, tree)
    run([*git, 'fetch', '--quiet', '--depth=1', '--no-tags', '--filter=blob:none', 'origin', spec['commit']], env, tree)
    require(run([*git, 'rev-parse', 'FETCH_HEAD^{commit}'], env, tree, capture=True) == spec['commit'],
            f'fetched {name} commit differs')
    # Path checkout can lazy-fetch one blob per round trip. Batch the exact blob IDs from
    # the pinned, allowlisted tree and forbid any further lazy fetch during materialization.
    blobs = run([*git, 'ls-tree', '-r', '--format=%(objectname)', spec['commit'], '--', *spec['paths']],
                env, tree, capture=True).splitlines()
    require(blobs, f'empty export: {name}')
    subprocess.run([*git, 'fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--recurse-submodules=no',
                    '--filter=blob:none', '--stdin', 'origin'], cwd=tree, env=env, text=True, check=True,
                   input='\n'.join(sorted(set(blobs))) + '\n')
    run([*git, '--no-lazy-fetch', 'checkout', spec['commit'], '--', *spec['paths']], env, tree)


def component(name, tmp, env):
    spec = LOCK['components'][name]
    tree = tmp / name
    checkout_component(tree, name, spec, env)
    shutil.rmtree(tree / '.git')  # Only the allowlisted runtime leaves staging, not the upstream bank/history.
    require(sha(tree / 'LICENSE') == LOCK['assets'][spec['license']], f'{name} license differs')
    require(export_digest(tree) == spec['digest'], f'{name} export differs')
    if name == 'memory-bank':
        run(['git', 'apply', ROOT / 'patches/memory-bank-profile.patch'], env, tree)
        require(export_digest(tree) == spec['installed_digest'], 'memory-bank profile patch differs')
    return tree


def mb_links(dest, full=False):
    profile = dest / 'profile'
    skills = profile / 'external-skills'
    mb = dest / ('plugin-pack/runtime/memory-bank' if full else 'memory-bank')
    return {**{skills / 'memory-bank' / f: mb / f for f in
               'SKILL.md LICENSE references docs scripts commands agents rules templates flow-templates hooks'.split()},
            skills / 'mb/SKILL.md': mb / 'commands/mb.md',
            **({} if full else {profile / 'plugins/memory-bank': mb / 'hermes/memory-bank'})}


def full_selection():
    assets = runpy.run_path(str(ROOT/'skill-pack/install.py'))
    manifest = assets['read_doc']()
    skills = manifest['skills']
    plugins = json.loads((ROOT/'plugin-pack/catalog.json').read_bytes())['registrations']
    return {
        'missing_skill_originals': [],  # no whole consolidation absent
        'not_redistributed_verbatim': manifest['distribution_exclusions'],
        'selected_skill_count': len(assets['selected'](skills)),
        'reading_only_skill_names': sorted(n for n, s in skills.items() if s['delivery'] in ('publisher-reading','owner-reading')),
        'not_selected_skill_variants': sorted(n for n,s in skills.items() if s.get('conflict') or s['delivery'] in ('publisher-reading','owner-reading')),
        'protected_skill_names': sorted(n for n, s in skills.items() if s.get('conflict')),
        'plugins_not_requested': sorted(PROFILE_HOLDBACK),
        'plugins_requested_without_enable': sorted(r['name'] for r in plugins if r['channel'] != 'host' and r['name'] not in PROFILE_HOLDBACK),
        'plugin_owned_skill_visibility': 'not verified', 'activation_verified': False,
    }


def full_config(dest, ui):
    # New file only. JSON is YAML; no profile settings, endpoints or grants are imported.
    config = {'skills': {'external_dirs': [str(dest/'profile/external-skills')]},
              'plugins': {'enabled': [], 'disabled': []}}
    if ui:
        config['display'] = {'skin': 'claude-graphite'}
    with (dest/'profile/config.yaml').open('x') as out:
        json.dump(config, out, indent=2)


def plugin_blobs(directory, catalog=False):
    require(directory.is_dir() and directory == directory.resolve(), 'missing or redirected plugin: '+str(directory))
    files = {}
    def fail(error):
        raise error
    for folder, dirs, names in os.walk(directory, onerror=fail):
        path = Path(folder)
        for d in dirs[:]:
            if d in ('.git', '__pycache__', '.deps') or (path/d).is_symlink():
                dirs.remove(d)
                if (path/d).is_symlink():
                    names.append(d)
        for name in names:
            p = path/name
            key = p.relative_to(directory).as_posix()
            if catalog and key == '.hermes-catalog.json':
                continue  # Written by the native PM; authoritative revision is in its sidecar outside the tree.
            if p.is_symlink():
                require(p.resolve().is_relative_to(directory), 'external source link: '+key)
                raw = os.fsencode(p.readlink())
            else:
                require(p.is_file(), 'not a source file: '+key)
                raw = p.read_bytes()
            files[key] = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def full_record(dest):
    assets = runpy.run_path(str(ROOT/'skill-pack/install.py'))
    skills = assets['read_doc']()['skills']
    names = sorted(assets['selected'](skills))
    for n in names:
        assets['checked_tree'](dest/'profile/skills'/n, skills[n].get('edition', skills[n]))
    directory = dest/'profile/plugins'
    metadata = directory/'.install-metadata.json'
    require(metadata.is_file() and metadata == metadata.resolve(), 'missing or redirected PM receipt')
    receipts = json.loads(metadata.read_bytes())
    plugin_hashes = {}
    for r in json.loads((ROOT/'plugin-pack/catalog.json').read_bytes())['registrations']:
        n = r['name']
        if n in PROFILE_HOLDBACK:
            require(not os.path.lexists(directory/n), 'unexpected excluded plugin: '+n)
        elif r['channel'] != 'host':
            row = receipts.get(n, {})
            require(row.get('pinned') is True and row.get('revision'), 'missing pinned PM receipt: '+n)
            digest = plugin_blobs(directory/n, bool(r.get('catalog')))
            if r['channel'] == 'upstream':
                require(row['revision'] == r['commit'] and digest == r['upstream_blobs_sha256'], 'upstream drift: '+n)
            else:
                require(digest == plugin_blobs(dest/'plugin-pack/packages'/n), 'assembled source drift: '+n)
            plugin_hashes[n] = digest
    return {'layout': 'profile', 'skills': names, 'plugins': plugin_hashes, 'held': PROFILE_HOLDBACK,
            'pm_metadata': sha(metadata),
            'inputs': {n: sha(ROOT/n) for n in ('plugin-pack/catalog.json','plugin-pack/files.sha256.json',
                       'plugin-pack/profile.defaults.json','review-skills/pins.json','skill-pack/manifest.json')},
            'selection': full_selection(), 'activation_verified': False}


def pack_command(group, args, env, cwd):
    run([sys.executable, '-IB', ROOT/group/'install.py', *args], env, cwd)


def finish_full(dest, env):
    pack_command('skill-pack', ['install', '--prefix', dest], env, dest/'home')
    pack_command('plugin-pack', ['install', 'all', '--profile', 'integrated', '--prefix', dest], env, dest/'home')
    record = full_record(dest)
    with (dest/FULL_RECEIPT).open('x') as out:
        json.dump(record, out, indent=2)
    (dest/PENDING).unlink()
    print('Installed admissible sources:', dest/'profile', '(not activated).')


def install(dest, tree, mb, wiki, wiki_python, bins, env, ui, executors=False, *, full=False):
    dest.mkdir(mode=0o700)
    if full:
        (dest/PENDING).touch(exist_ok=False)
    repo = dest / 'source'
    shutil.move(tree, repo)
    shutil.move(mb, dest / 'memory-bank')
    for folder in ('tool-bin', 'home', 'tmp', 'profile'):
        (dest / folder).mkdir()
    (dest / 'tool-bin/jq').symlink_to(bins['jq'])
    env['UV_PROJECT_ENVIRONMENT'] = str(dest / 'venv')
    run([bins['uv'], '--no-config', 'sync', '--frozen', '--no-default-groups', '--no-python-downloads',
         '--python', sys.executable], env, repo)
    launcher = copy_launcher(dest, 'workbench')
    if ui:
        (dest / 'profile/skins').mkdir()
        install_ui(dest, repo, bins, env)
        if not full:
            run([launcher, 'config', 'set', 'display.skin', 'claude-graphite'], env, dest / 'home')
            run([sys.executable, '-I', ROOT / 'tests/smoke.py', dest], env, dest / 'home')
    wiki_runtime(dest, wiki, bins['uv'], wiki_python, env)
    if full:
        full_config(dest, ui)
        pack = runpy.run_path(str(ROOT/'plugin-pack/install.py'))
        pack['verify']()
        store = dest/'plugin-pack'
        store.mkdir()
        pack['runtimes'](dest, store)
    wiki_root = dest / 'profile/llm-wiki'
    for args in [('init',), ('index', '--lexical-only')]:
        run([dest / 'llm-wiki', *args, '--root', wiki_root], env, dest / 'home')
    bank_runtime = dest / ('plugin-pack/runtime/memory-bank' if full else 'memory-bank')
    run(['bash', bank_runtime / 'scripts/mb-init-bank.sh', '--project-root=' + str(dest / 'profile')],
        {**env, 'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_HOME': str(dest/'profile'), 'HOME': str(dest/'home')}, dest / 'home')
    if executors:
        (dest / 'executors/bin').mkdir(parents=True)
    for p in [*([] if full else LOCK['assets']), *(json.loads(EXEC_LOCK.read_text()) if executors else ())]:
        if p.startswith('plugins/'):
            install_path = dest / 'profile' / p
            install_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / p, install_path)
    for link, obj in mb_links(dest, full).items():
        link.parent.mkdir(exist_ok=True, parents=True)
        link.symlink_to(obj)
    if full:
        receipt_for(dest, 'tui' if ui else 'base', executors, full=True)
        finish_full(dest, env)
        return
    dirs = json.loads(run([launcher, 'config', 'get', 'skills.external_dirs', '--json'], env, dest / 'home', capture=True))
    require(isinstance(dirs, list), 'skills.external_dirs is not a list')
    external = str(dest / 'profile/external-skills')
    if external not in dirs:
        run([launcher, 'config', 'set', 'skills.external_dirs', json.dumps([*dirs, external])], env, dest / 'home')
    for plugin in ('memory-bank', 'llm-wiki'):
        run([launcher, 'plugins', 'enable', plugin], env, dest / 'home')
    if executors:
        run([launcher, 'plugins', 'enable', 'workbench-executors'], env, dest / 'home')
        run([launcher, 'executor', '--help'], env, dest / 'home')
    run([sys.executable, '-I', ROOT / 'tests/smoke_mb.py', dest], env, dest / 'home')
    receipt_for(dest, 'tui' if ui else 'base', executors)
    print('Installed:', launcher)


def prepare_wiki(tmp, env):
    py = shutil.which('python3.11', path=env['PATH'])
    require(py and run([py, '-I', '-c', 'import sys; print(sys.version_info[:2])'], env, tmp, capture=True)
            == '(3, 11)', 'Wiki requires python3.11 on PATH')
    return component('llm-wiki', tmp, env), py


def wiki_runtime(dest, tree, uv, py, env):
    source = dest / 'llm-wiki-src'
    shutil.move(tree, source)
    run([uv, '--no-config', 'venv', '--no-python-downloads', '--python', py, dest / 'wiki-venv'], env, dest)
    run([uv, '--no-config', 'pip', 'sync', '--python', dest / 'wiki-venv/bin/python', source / 'requirements.lock'], env, dest)
    return copy_launcher(dest, 'llm-wiki')


def wiki_install(dest, tree, uv, py, env):
    dest.mkdir(mode=0o700)
    for folder in ('home', 'tmp'):
        (dest / folder).mkdir()
    launcher = wiki_runtime(dest, tree, uv, py, env)
    run([sys.executable, '-I', ROOT / 'tests/smoke_wiki.py', dest], env, dest / 'home')
    receipt_for(dest, 'wiki')
    print('Installed (no model downloaded):', launcher)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True, help='new directory under an existing parent')
    parser.add_argument('--source', type=Path, help='read-only local Git checkout; uses only committed objects')
    parser.add_argument('--check', action='store_true', help='verify source, assets and tools in temp; do not install')
    parser.add_argument('--full-profile', action='store_true', help='install both profile packs together, without enabling external plugins')
    parser.add_argument('--with-executors', action='store_true', help='add local user-invoked coding CLI adapters (macOS)')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--with-tui', action='store_true', help='also build the optional patched TUI and Graphite')
    modes.add_argument('--wiki', action='store_true', help='install only the standalone Wiki CLI into its own prefix')
    args = parser.parse_args()
    os.umask(0o077)
    source = args.source.expanduser().resolve(strict=True) if args.source else None
    dest = target(args.prefix, source)
    require(not (source and args.wiki), '--source applies only to Hermes')
    require(not (args.full_profile and args.wiki), 'cannot combine --full-profile and --wiki')
    require(not args.with_executors or (not args.wiki and sys.platform == 'darwin'),
            '--with-executors requires a macOS Hermes installation')
    if args.full_profile:
        print('Full-profile selection (not a receipt):', json.dumps(full_selection(), sort_keys=True), flush=True)
    for path, digest in (LOCK['assets'] | (json.loads(EXEC_LOCK.read_text()) if args.with_executors else {})).items():
        require(sha(ROOT / path) == digest, f'distribution asset checksum differs: {path}')
    if dest.exists():
        verify_existing(dest, 'wiki' if args.wiki else 'tui' if args.with_tui else 'base', args.with_executors, full=args.full_profile)
        return
    with tempfile.TemporaryDirectory(prefix='workbench-') as temp:
        tmp = Path(temp).resolve()
        home = tmp / 'home'
        home.mkdir()
        env = environment(home, tmp)
        if args.full_profile:
            pack_command('skill-pack', ['verify'], env, tmp)
            pack_command('plugin-pack', ['inventory'], env, tmp)
        if args.wiki:
            uv = tool('uv', 'uv ' + LOCK['uv'], env, tmp)
            wiki, py = prepare_wiki(tmp, env)
            if not args.check:
                wiki_install(dest, wiki, uv, py, env)
                return
        else:
            repo, bins = prepare(source, tmp, env, args.with_tui)
            mb = component('memory-bank', tmp, env)
            wiki, py = prepare_wiki(tmp, env)
            if not args.check:
                install(dest, repo, mb, wiki, py, bins, env, args.with_tui, args.with_executors, full=args.full_profile)
                return
        print('Preflight passed; prefix not created.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(f'Workbench: {exc}')
