"""Make an opt-in PRIVATE review copy of allowlisted local documents. Never installs or publishes."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('wb_skill_pack',HERE/'install.py');a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profile',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    x=p.parse_args(); root=x.profile; dest=x.out; a.require(root.is_absolute() and root.resolve(strict=True)==root,'use a canonical profile directory')
    a.require(dest.is_absolute() and dest==dest.resolve() and not dest.exists() and dest.parent.is_dir(), 'choose a new private directory')
    a.require(not dest.is_relative_to(root) and not dest.is_relative_to(HERE.parent) and not any((p/'.git').exists() for p in dest.parents), 'never export into a profile, Workbench, or git checkout')
    d=a.read_doc(); records={};raw={}
    for n,s in d['skills'].items():
        if 'local_review_member' not in s: continue
        folder=root/'skills'/a.relpath(s['local_review_member']);a.require(folder.resolve(strict=True)==folder and folder.is_dir(),'skill root missing or redirected: '+n)
        selected={}
        for f in folder.rglob('*'):
            a.require(not f.is_symlink(),'symlink refused: '+n)
            if f.is_dir():continue
            # For local curated skills, copy their previously inventoried Markdown only. Never
            # export profile records, logs, config, caches, executables or arbitrary future additions.
            if s['delivery'] in ('owner-release','owner-reading'):
                if f.suffix!='.md':continue
            elif f.relative_to(folder).as_posix()!='SKILL.md':continue
            k=a.relpath(f.relative_to(folder).as_posix()); a.require(f.is_file() and f.stat().st_size<=1_000_000,'unexpected member: '+n)
            b=f.read_bytes();a.require(b'\x00' not in b and not a.SECRET.search(b),'possible sensitive material, export refused: '+n)
            selected[k]=b
        names_hash=hashlib.sha256('\n'.join(sorted(selected)).encode()).hexdigest()
        a.require(len(selected)==s['private_review_files'] and names_hash==s['private_member_names_digest'],
                  'inventory drift, review before export: '+n)
        raw[n]=selected;records[n]={k:hashlib.sha256(b).hexdigest() for k,b in selected.items()}
    os.umask(0o077);dest.mkdir(mode=0o700)
    for n,files in raw.items():
        for k,b in files.items():
            path=dest/n/k;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with path.open('xb') as f:f.write(b)
            a.require(hashlib.sha256(path.read_bytes()).hexdigest()==records[n][k],'private copy failed')
    (dest/'private-receipt.json').write_text(json.dumps(records,indent=2)+'\n');print(json.dumps({'private_skills_or_variants':len(raw),'private_files':sum(map(len,raw.values())),'installed':0,'published':0}))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as e:sys.exit('Private review: '+str(e))
