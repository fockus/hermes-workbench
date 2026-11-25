"""Offline source proof. Pass local Git mirrors; never read a profile or execute a skill.

Usage: python3 -IB skill-pack/provenance.py --repo OWNER/PROJECT=/path/to/clone ...
Optional: --providers /path/to/plugin-pack/catalog.json checks ownership/delegated delivery.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('workbench_skill_pack',HERE/'install.py')
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


def readgit(repo,*args):
    return a.git(repo,'--no-lazy-fetch',*args)


def verify_sources(d,repos):
    a.require(set(repos)==set(d['sources']),'supply exactly one git mirror per manifest source')
    for id,s in d['sources'].items():
        a.require(readgit(repos[id],'cat-file','-t',s['commit']).strip()==b'commit','not a commit: '+id)
    def rev(id):return d['sources'][id]['commit']
    def tree(id,path):return readgit(repos[id],'rev-parse',rev(id)+('^{tree}' if path=='.' else ':'+path)).decode().strip()
    for n,s in d['skills'].items():
        if h:=s.get('host_variant'):
            a.require(tree(h['source'],h['path'])==h['tree'],'host tree mismatch: '+n)
        if s['delivery']=='base-component':
            a.require(a.sha(readgit(repos[s['source']],'show',rev(s['source'])+':'+s['path']))==s['document_sha256'],'base document: '+n)
        if 'files' not in s:continue
        id=s['source']; repo=repos[id]; a.require(tree(id,s['path'])==s['tree'],'skill tree mismatch: '+n)
        observed={}; executable=set()
        for line in readgit(repo,'ls-tree','-rz',s['tree']).split(b'\0'):
            if not line:continue
            header,k=line.split(b'\t',1); k=k.decode();mode,kind,oid=header.decode().split()
            if s.get('source_members') and k.split('/')[0] not in s['source_members']:continue
            a.require(kind=='blob' and mode in ('100644','100755'),'non-regular member: '+n)
            observed[k]=a.sha(readgit(repo,'cat-file','blob',oid))
            if mode=='100755':executable.add(k)
        for k,note in s['notices'].items():
            id=note['source']; digest=a.sha(readgit(repos[id],'show',rev(id)+':'+note['path']))
            a.require(observed.get(k,digest)==digest==note['sha256'],'notice: '+n);observed[k]=digest
        a.require(observed==s['files'] and executable==set(s['executable']),'incomplete source/mode proof: '+n)


def providers(d,path):
    catalog=json.loads(path.read_bytes()); reg={v['name']:v for v in catalog['registrations']}
    owners={v['name']:v['owner'] for v in catalog['separate']['plugin-skill']};out={}
    for n,s in d['skills'].items():
        if s['delivery']!='plugin-owned':continue
        owner=s['plugin'];a.require(owners.get(n)==owner and owner in reg,'unmapped provider: '+n);r=reg[owner];channel=r['channel']
        if channel=='upstream':a.require(r['commit']==s['provider_commit'],'provider version changed: '+n)
        elif channel=='packaged':
            a.require(catalog['source_provenance'][owner]['commit']==s['provider_commit'],'different packaged source: '+n)
            for k,digest in s['provider_files'].items():
                a.require(a.sha((path.parent/r['path']/k).read_bytes())==digest,'packaged provider bytes differ: '+n)
        else: channel='not-delivered'
        out[n]=channel
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',action='append',default=[],metavar='OWNER/PROJECT=PATH');p.add_argument('--providers',type=Path);x=p.parse_args();d=a.read_doc();a.verify(d)
    if not x.repo and not x.providers:p.error('supply source mirrors or --providers')
    out={}
    if x.repo:
        repos={k:Path(v).resolve(strict=True) for k,v in (it.split('=',1) for it in x.repo)};verify_sources(d,repos);out['source_mirrors_verified']=len(repos)
    if x.providers:out['provider_channels']=providers(d,x.providers)
    print(json.dumps(out,sort_keys=True))


if __name__=='__main__':
    try:main()
    except (KeyError,OSError,ValueError,subprocess.SubprocessError) as e:sys.exit('Source proof: '+str(e))
