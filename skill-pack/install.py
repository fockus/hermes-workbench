#!/usr/bin/env python3
"""Verify the public skill inventory or install its admissible portion into one trusted Workbench."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REVIEW = ROOT / 'review-skills'
MANIFEST = HERE / 'manifest.json'
NAME = re.compile(r'[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)?\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
OID = re.compile(r'[a-f0-9]{40}\Z')
SECRET = re.compile(rb'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|sk-(?:proj-)?[A-Za-z0-9_-]{40,}|AKIA[A-Z0-9]{16}')


def require(value, message):
    if not value:
        raise ValueError(message)


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relpath(value):
    p = PurePosixPath(value)
    require(value and not p.is_absolute() and str(p) == value and not any(x in {'.', '..', '.git'} for x in p.parts)
            and '\\' not in value and ':' not in value and not any(ord(c) < 32 for c in value), 'invalid member path')
    return value


def read_doc():
    d = json.loads(MANIFEST.read_bytes())
    skills = d['skills']; counts = Counter(s['delivery'] for s in skills.values())
    require(d['schema'] == 1 and len(skills) == d['baseline']['union'] == 90 and
            sum(s['visible'] for s in skills.values()) == d['baseline']['visible'] == 89 and
            counts == d['counts'] and [n for n,s in skills.items() if not s['visible']] == ['sdlc-review'], 'inventory mismatch')
    require(all(NAME.fullmatch(n) for n in skills), 'invalid skill identifier')
    for source, pin in d['sources'].items():
        require(re.fullmatch(r'[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+', source) and pin['repo'] == 'https://github.com/'+source+'.git'
                and OID.fullmatch(pin['commit']), 'invalid source pin')
    require(d['sources']['NousResearch/hermes-agent']['commit'] == json.loads((ROOT/'workbench.lock.json').read_bytes())['commit'], 'different host pin')
    for n,s in skills.items():
        if 'files' in s:
            require(OID.fullmatch(s['tree']) and s['source'] in d['sources'] and 'SKILL.md' in s['files'] and s['notices'], 'incomplete pin: '+n)
            require(set(s['executable']) <= set(s['files']) and all(relpath(k) and SHA.fullmatch(v) for k,v in s['files'].items()), 'invalid file pin: '+n)
            require(s['path']=='.' or relpath(s['path']), 'invalid source path')
            for name,note in s['notices'].items():
                require(note['source'] in d['sources'] and relpath(note['path']) and note['sha256'] == s['files'][name], 'invalid notice: '+n)
        if s['delivery']=='plugin-owned':
            require(n.split(':')[0] == s['plugin'] and s['provider_path'] in s['provider_files'] and OID.fullmatch(s['provider_commit']), 'invalid plugin mapping')
    return d


def checked_tree(directory, spec, extra=None):
    files = {}; extra = extra or {}
    require(directory == directory.resolve(strict=True) and directory.is_dir(), 'redirected payload')
    for p in directory.rglob('*'):
        require(not p.is_symlink(), 'symlink in payload')
        if p.is_dir():
            require(any(k.startswith(p.relative_to(directory).as_posix()+'/') for k in spec['files']), 'unexpected payload directory')
            continue
        k = p.relative_to(directory).as_posix()
        require(k in spec['files'] and p.is_file() and bool(p.stat().st_mode & 0o111) == (k in spec['executable']), 'unexpected payload member or mode')
        files[k] = p.read_bytes()
    require(not files.keys() & extra.keys(), 'notice collides with source')
    files.update(extra)
    return checked_bytes(files,spec)


def checked_bytes(files,spec):
    require(set(files) == set(spec['files']), 'incomplete or extra source members')
    for k,b in files.items():
        require(sha(b) == spec['files'][k] and not SECRET.search(b), 'payload checksum or credential check failed')
    return files


def git_tree(files, executable):
    """Reconstruct Git tree objects with Git's directory sort order (without invoking git)."""
    def obj(kind,data):
        return hashlib.sha1(kind+b' '+str(len(data)).encode()+b'\0'+data).digest()
    root = {}
    for rel,b in files.items():
        tree = root; parts = rel.split('/')
        for part in parts[:-1]: tree = tree.setdefault(part,{})
        tree[parts[-1]] = (b'100755' if rel in executable else b'100644',obj(b'blob',b))
    def walk(tree):
        entries=[]
        for name,v in tree.items():
            k=name.encode(); isdir=isinstance(v,dict); mode,oid=(b'40000',walk(v)) if isdir else v
            entries.append((k+(b'/' if isdir else b''),mode+b' '+k+b'\0'+oid))
        return obj(b'tree',b''.join(v for _,v in sorted(entries)))
    return walk(root).hex()


def git(cwd,*args):
    env={'PATH':os.defpath,'HOME':str(cwd),'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1',
         'GIT_TERMINAL_PROMPT':'0','GIT_ALLOW_PROTOCOL':'https','LC_ALL':'C'}
    return subprocess.check_output(['git','-c','credential.helper=','-c','core.hooksPath='+os.devnull,
                                    '-c','init.templateDir=','-C',str(cwd),*args],env=env,timeout=240,stderr=subprocess.PIPE)


def resolve_repo(source,doc,scratch):
    pin=doc['sources'][source]; repo=scratch/pin['commit']
    if not repo.exists():
        repo.mkdir(mode=0o700); git(repo,'init','--quiet','--bare'); git(repo,'fetch','--quiet','--no-tags','--depth=1',pin['repo'],pin['commit'])
    require(git(repo,'rev-parse','FETCH_HEAD^{commit}').decode().strip()==pin['commit'],'wrong upstream revision')
    return repo


def upstream_files(spec,doc,scratch):
    repo=resolve_repo(spec['source'],doc,scratch); commit=doc['sources'][spec['source']]['commit']
    path=spec['path']; revision=commit+'^{tree}' if path=='.' else commit+':'+path
    require(git(repo,'rev-parse',revision).decode().strip()==spec['tree'],'source tree identity mismatch')
    raw={}; modes=set()
    for line in git(repo,'ls-tree','-rz',spec['tree']).split(b'\0'):
        if not line: continue
        h,k=line.split(b'\t',1); mode,kind,oid=h.decode().split(); k=relpath(k.decode())
        if spec.get('source_members') and k.split('/')[0] not in spec['source_members']: continue
        require(kind=='blob' and mode in {'100755','100644'} and k in spec['files'],'unexpected source member')
        raw[k]=git(repo,'cat-file','blob',oid)
        if mode=='100755': modes.add(k)
    require(modes==set(spec['executable']), 'source mode mismatch')
    for k,note in spec['notices'].items():
        p=resolve_repo(note['source'],doc,scratch); rev=doc['sources'][note['source']]['commit']
        blob=git(p,'show',rev+':'+note['path']); require(k not in raw or raw[k]==blob,'source notice collision');raw[k]=blob
    return checked_bytes(raw,spec)


def pack_files(n,s,d,scratch=None):
    mode=s['delivery']
    if 'edition' in s:
        edition=s['edition']; inert=mode in ('owner-reading','publisher-reading')
        files=checked_tree(HERE/('reading-only' if inert else 'editions')/n,edition)
        require(git_tree(files,set(edition['executable']))==edition['tree'],'edition tree differs: '+n)
        if mode.startswith('owner-'):
            require(files['LICENSE']==(ROOT/'LICENSE').read_bytes(),'missing Workbench notice: '+n)
        elif mode=='publisher-reading':
            checked_bytes(files,s)
        elif mode=='publisher-edition':
            require(files['LICENSE-Workbench']==(ROOT/'LICENSE').read_bytes(),'missing edition notice')
            written=set(edition['independently_written'])
            require(written <= s['files'].keys() and not written.intersection(s['notices']) and
                    set(files)==set(s['files'])|{'LICENSE-Workbench'} and edition['executable']==s['executable'], 'edition members or notices differ')
            for path,digest in s['files'].items():
                if path not in written: require(sha(files[path])==digest,'changed publisher member: '+path)
            if scratch is not None: upstream_files(s,d,scratch)
        else: raise ValueError('unknown edition channel: '+mode)
        return files
    if mode=='source-only':
        return upstream_files(s,d,scratch) if scratch is not None else None
    if mode=='vendored': files=checked_tree(HERE/'payload'/n,s)
    elif mode=='review-pack': files=checked_tree(REVIEW/'payload'/n,s,{'LICENSE':(ROOT/'LICENSES/Superpowers.txt').read_bytes()})
    else: return None
    original={k:b for k,b in files.items() if k not in s['notices'] or k in s['source_notices_in_tree']}
    if not s.get('source_members'):
        require(git_tree(original,set(s['executable']))==s['tree'],'not a complete upstream tree: '+n)
    return files


def selected(skills):
    active={n:s for n,s in skills.items() if s['delivery'] in ('vendored','review-pack','owner-release','publisher-edition') and not s.get('conflict')}
    require(not set(active).intersection({'systematic-debugging','mlops-model-lifecycle','requesting-code-review','test-driven-development'}),'reserved name selected')
    return active


def bundle_source(n,s,d):
    if 'edition' in s:
        tree=s['edition']['tree']
        return 'workbench','urn:workbench:skill:'+n+':git-tree:'+tree,tree,'workbench'
    source=s['source']; c=d['sources'][source]['commit']
    return source,f'https://github.com/{source}/tree/{c}'+('/'+s['path'] if s['path']!='.' else ''),c,'github'


def verify(d,scratch=None):
    pins=json.loads((REVIEW/'pins.json').read_bytes()); expected=d['skills'];
    require(set(pins['skills'])=={n for n,s in expected.items() if s['delivery']=='review-pack'} and pins['commit']==d['sources']['obra/superpowers']['commit'],'review pack drift')
    require({p.name for p in (HERE/'payload').iterdir()}=={n for n,s in expected.items() if s['delivery']=='vendored'},'unexpected distribution directories')
    require(sha((HERE/'notices/talkstream-ru-text-MIT.txt').read_bytes())==expected['writing-clearly']['files']['LICENSE-ru-text'],'third-party notice changed')
    for folder,modes in [('reading-only',('owner-reading','publisher-reading')),('editions',('owner-release','publisher-edition'))]:
        require({p.name for p in (HERE/folder).iterdir()}=={n for n,s in expected.items() if s['delivery'] in modes}, 'unexpected '+folder+' directories')
    require(len(selected(expected))==d['static_admission']['selected'],'selection differs')
    require(expected['mlops-model-lifecycle']['edition']['files']['LICENSE-Orchestra']==sha((HERE/'recovery/upstream-reading/Orchestra-Research/LICENSE').read_bytes()),'Orchestra notice differs')
    for n,s in expected.items(): pack_files(n,s,d,scratch)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('action',choices=('verify','install'))
    p.add_argument('--prefix',type=Path,help='absolute Workbench prefix (install only)')
    p.add_argument('--sources',action='store_true',help='also verify the two original publisher inputs (not installed)')
    args=p.parse_args(); d=read_doc()
    if args.action=='verify':
        if args.prefix:p.error('verify does not use a profile')
        if args.sources:
            with tempfile.TemporaryDirectory(prefix='workbench-skills-') as tmp:verify(d,Path(tmp))
        else:verify(d)
        print(json.dumps({'union':len(d['skills']),'visible':d['baseline']['visible'],'delivery':d['counts'],'selected':len(selected(d['skills'])),'original_sources_checked':args.sources},sort_keys=True));return
    if args.sources or not args.prefix or not args.prefix.is_absolute():p.error('install requires only --prefix ABSOLUTE_PATH')
    root=args.prefix; load_module('workbench_review_prefix',REVIEW/'review.py').validate_prefix(root); verify(d)
    env={'PATH':str(root/'tool-bin')+':'+os.defpath,'HOME':str(root/'home'),'HERMES_HOME':str(root/'profile'),'TMPDIR':str(root/'tmp'),'LANG':'C.UTF-8'}
    subprocess.run([root/'venv/bin/python','-IB',HERE/'worker.py',str(root)],env=env,cwd=root/'home',check=True)


if __name__=='__main__':
    try: main()
    except (ValueError,KeyError,OSError,subprocess.SubprocessError) as e: sys.exit('Skill pack: '+str(e))
