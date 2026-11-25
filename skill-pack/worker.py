"""Native Skills Hub admission; invoked only by install.py's isolated prefix interpreter."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('workbench_skill_assets', HERE/'install.py')
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)
require = assets.require


def install(root):
    profile = root/'profile'; hub = profile/'skills/.hub'
    require(root == root.resolve(strict=True) and Path(sys.prefix) == root/'venv' and
            Path.home() == Path.cwd() == root/'home' and Path(os.environ['HERMES_HOME']) == profile,
            'use the prefix entrypoint')
    assets.load_module('workbench_review_prefix',assets.REVIEW/'review.py').validate_prefix(root)
    for path in [profile, profile/'config.yaml', profile/'skills', hub, profile/'external-skills']:
        require(path == path.resolve(), 'redirected profile state')
    require(not any(p.is_symlink() for p in hub.rglob('*')), 'redirected hub state')
    from hermes_cli.config import load_config
    from tools.skills_guard import scan_skill_cached, should_allow_install
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_install import (quarantine_bundle, discard_quarantine, hub_install_lock,
                                         install_from_quarantine)
    from tools.skills_hub_models import SkillBundle
    from tools.skills_tool import _skill_catalog

    for path in (load_config().get('skills') or {}).get('external_dirs') or []:
        require(Path(path).expanduser().resolve().is_relative_to(profile), 'external skill root outside this profile')
    doc = assets.read_doc(); skills = doc['skills']; assets.verify(doc)
    active = assets.selected(skills)
    # Dependencies are installed before dependants. Batch admission is all-or-nothing; an I/O
    # error during the subsequent native moves is not a transaction. Repeats never repair edits.
    order = sorted(active, key=lambda n:(bool(active[n].get('requires_skill')), n))
    require(all(s.get('requires_skill') in active for s in active.values() if s.get('requires_skill')), 'missing sibling dependency')
    ready = []; present = 0
    with tempfile.TemporaryDirectory(prefix='skill-pack-', dir=root/'tmp') as tmp, hub_install_lock(), ExitStack() as cleanup:
        catalog = _skill_catalog(skip_disabled=True,include_hidden=True)
        for n in order:
            s = active[n]; target = profile/'skills'/n; material=s.get('edition',s)
            source,url,c,kind = assets.bundle_source(n,s,doc)
            data = assets.pack_files(n,s,doc); b=SkillBundle(n,data,kind,url,'community',{'source_revision':c,'source_url':url})
            paths = [row['path'] for row in catalog if row['name']==n]; entry=HubLockFile().get_installed(n)
            if paths or entry or target.exists() or target.is_symlink():
                require(paths==[target/'SKILL.md'] and target==target.resolve() and entry and
                        entry.get('identifier')==url and entry.get('trust_level')=='community' and
                        entry.get('scan_verdict')=='safe' and entry.get('install_path')==n,
                        'existing skill or different provenance, not overwritten: '+n)
                assets.checked_tree(target,material); present+=1
                continue
            q=quarantine_bundle(b); cleanup.callback(discard_quarantine,q)
            for rel in material['executable']: (q/rel).chmod(0o700)
            assets.checked_tree(q,material)
            # An invocation-private scan cache prevents an old profile cache entry from
            # standing in for this installation's fresh scan of the exact quarantined bytes.
            scan,provenance=scan_skill_cached(q,source=source,source_url=url,trust_level='community',cache_dir=Path(tmp)/'scan')
            allowed,_=should_allow_install(scan)
            require(allowed is True and scan.verdict=='safe', 'host denied '+n+': '+scan.verdict)
            ready.append((b,q,scan,provenance,material))
        for b,q,scan,provenance,s in ready:
            target=install_from_quarantine(q,b.name,'',b,scan,provenance)
            assets.checked_tree(target,s)
    print(json.dumps({'installed_now':len(ready),'already_matched':present,'selected':len(active),
                      'reserved_host_names':sorted(n for n,s in skills.items() if s.get('conflict')),
                      'reading_only':sorted(n for n,s in skills.items() if s['delivery'] in ('owner-reading','publisher-reading')),
                      'provider_skills_managed_separately':sum(s['delivery']=='plugin-owned' for s in skills.values())},sort_keys=True))


if __name__=='__main__':
    try: install(Path(sys.argv[1]))
    except (OSError,ValueError,KeyError) as e: sys.exit('Skill pack: '+str(e))
