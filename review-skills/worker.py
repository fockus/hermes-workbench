"""Pinned-host worker. The public entry point is review.py."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def require(ok, message):
    if not ok:
        raise ValueError(message)


def checked_file(path, digest):
    require(path == path.resolve() and path.is_file(), 'unexpected payload path')
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == digest, 'payload checksum mismatch')
    return data


def exact_tree(directory, files, executable, error):
    """Check every entry, including nested helper files and their executable bits."""
    dirs = {d.as_posix() for rel in files for d in Path(rel).parents if d != Path('.')}
    seen = set()
    for item in directory.rglob('*'):
        rel = item.relative_to(directory).as_posix()
        require(not item.is_symlink(), error)
        if rel in dirs:
            require(item.is_dir(), error)
        else:
            require(rel in files and item.is_file() and item.read_bytes() == files[rel]
                    and bool(item.stat().st_mode & 0o111) == (rel in executable), error)
        seen.add(rel)
    require(seen == dirs | files.keys(), error)


def run(action, root, group):
    profile = Path(os.environ['HERMES_HOME'])
    base = profile.parent
    require(profile == base / 'profile' and profile.resolve(strict=True) == profile and
            (base == root or (base.parent == root / 'tmp' and base.name.startswith('review-smoke-'))) and
            Path(sys.prefix) == root / 'venv' and Path.home() == base / 'home' and Path.cwd() == Path.home(),
            'use the isolated review.py entry point')
    require(action in ('absent', 'install', 'check'), 'unknown operation')
    from hermes_cli.config import load_config
    from tools.skills_guard import scan_skill_cached, should_allow_install
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_install import (discard_quarantine, hub_install_lock, install_from_quarantine,
                                         quarantine_bundle)
    from tools.skills_hub_models import SkillBundle
    from tools.skills_tool import _skill_catalog, skills_list, skill_view

    for path in (profile / 'config.yaml', profile / 'skills', profile / 'skills/.hub'):
        require(path == path.resolve(), 'redirected profile state')
    hub = profile / 'skills/.hub'
    require(not any(p.is_symlink() for p in hub.rglob('*')), 'redirected hub state')
    # A shared external search root could expose an unrelated profile through skill lookup.
    for d in (load_config().get('skills') or {}).get('external_dirs') or []:
        require(Path(d).resolve().is_relative_to(profile), 'external skill root outside selected profile')
    pins = json.loads((HERE / 'pins.json').read_bytes())
    license_text = checked_file(HERE.parent / 'LICENSES/Superpowers.txt', pins['license_sha256'])
    require(group and set(group) <= pins['skills'].keys() and len(set(group)) == len(group), 'unknown selection')

    def bundle(name):
        spec = pins['skills'][name]
        directory = HERE / 'payload' / name
        files = {rel: checked_file(directory / rel, sha) for rel, sha in spec['files'].items()}
        exact_tree(directory, files, spec['executable'], 'unexpected distribution tree')
        files['LICENSE'] = license_text
        url = f"https://github.com/{pins['repo']}/tree/{pins['commit']}/skills/{name}"
        return SkillBundle(name, files, 'github', url, 'community', {'commit': pins['commit'], 'license': 'MIT'})

    def visible():
        result = json.loads(skills_list())
        require(result.get('success'), 'host discovery failed')
        return [s['name'] for s in result['skills']]

    with hub_install_lock(), ExitStack() as stack:
        catalog = _skill_catalog(skip_disabled=True, include_hidden=True)  # ignore the disabled filter for collisions
        names = visible()
        ready = []
        for name in group:
            b = bundle(name)
            target = profile / 'skills' / name
            paths = [x['path'] for x in catalog if x['name'] == name]
            entry = HubLockFile().get_installed(name)
            exists = bool(paths or entry or target.exists() or target.is_symlink() or name in names)
            if action == 'absent':
                require(not exists, 'test profile already has a group member')
                continue
            if exists:
                require(paths == [target / 'SKILL.md'] and target.resolve() == target and
                        entry and entry.get('identifier') == b.identifier and entry.get('scan_verdict') == 'safe' and
                        entry.get('trust_level') == 'community' and entry.get('install_path') == name,
                        'skill collision or different hub provenance; nothing overwritten')
                exact_tree(target, b.files, pins['skills'][name]['executable'], 'installed payload drift')
                require(names.count(name) == 1, 'skill disabled, shadowed or duplicated')
                view = json.loads(skill_view(name, preprocess=False))
                require(view.get('success') and view['content'] == b.files['SKILL.md'].decode() and
                        Path(view['skill_dir']) == target, 'host returned a different skill')
                if action == 'check':
                    from agent.skill_commands import build_skill_invocation_message, resolve_skill_command_key
                    key = resolve_skill_command_key(name)
                    require(key == '/' + name, 'slash command not registered')
                    marker = 'WorkbenchReviewFixture'
                    message = build_skill_invocation_message(key, marker)
                    require(message and marker in message and view['content'].strip() in message,
                            f'slash rendering failed: {name}')
                continue
            require(action == 'install', 'skill not installed')
            q = quarantine_bundle(b)
            stack.callback(discard_quarantine, q)
            # SkillBundle carries bytes, not POSIX modes. Set upstream execution bits
            # on the new private staging files before the native guard sees them.
            for rel in pins['skills'][name]['executable']:
                (q / rel).chmod(0o700)
            scan, attestation = scan_skill_cached(q, source=pins['repo'], source_url=b.identifier,
                                                 trust_level='community', cache_dir=hub / 'scan-cache')
            allowed, _ = should_allow_install(scan)
            require(allowed is True and scan.verdict == 'safe', f'host admission: {name}: {scan.verdict}')
            ready.append((b, q, scan, attestation))
        for b, q, scan, attestation in ready:  # no group member published until all checks have passed
            install_from_quarantine(q, b.name, '', b, scan, attestation)
        print(json.dumps({'action': action, 'visible_before': len(names), 'group_before': sum(n in names for n in group),
                          'installed_now': len(ready)}))


if __name__ == '__main__':
    try:
        run(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])
    except (OSError, ValueError) as e:
        sys.exit(f'Review skills: {e}')
