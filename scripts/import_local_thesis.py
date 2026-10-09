"""Review and copy selected thesis sources; never touch Git or overwrite files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from check_publication import violations

ROOT=Path(__file__).resolve().parents[1]
SKIP_DIRS={'.git','.venv','venv','env','node_modules','__pycache__','.ipynb_checkpoints','.openai','.vscode','.idea','RTS_Data','RTS-GMLC','raw','tmp','scratch'}
ALLOWED_DIRS={'src','notebooks','scripts'}
SOURCE_SUFFIXES={'.py','.ipynb','.md','.txt','.toml','.yaml','.yml'}
ROOT_FILES={'README.md','README.txt','requirements.txt','pyproject.toml','environment.yml'}

def cleaned_bytes(path):
    if path.stat().st_size>10*1024*1024:raise ValueError('larger than 10 MiB; review separately')
    text=path.read_text(encoding='utf-8')
    if path.suffix=='.ipynb':
        nb=json.loads(text)
        nb['metadata']={k:v for k,v in nb.get('metadata',{}).items() if k in ['kernelspec','language_info']}
        for cell in nb.get('cells',[]):
            cell['metadata']={}
            if cell.get('cell_type')=='code':cell['outputs']=[];cell['execution_count']=None
        text=json.dumps(nb,indent=1,ensure_ascii=False)+'\n'
    if violations(text):raise ValueError('possible credential signature; not copied')
    return text.encode('utf-8')

def plan(source,target):
    records=[];payloads={}
    for directory,dirs,files in os.walk(source,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.') and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            p=Path(directory)/name;rel=p.relative_to(source)
            rec={'source':str(rel),'status':'skip'}
            selected=(len(rel.parts)>1 and rel.parts[0] in ALLOWED_DIRS and p.suffix in SOURCE_SUFFIXES) or (len(rel.parts)==1 and (name in ROOT_FILES or p.suffix in {'.py','.ipynb'}))
            if p.is_symlink():rec['reason']='symlink'
            elif not selected:rec['reason']='outside selected source types/directories'
            elif name.startswith('.') or any(word in name.lower() for word in ['credential','secret','token','password']):rec['reason']='possible credential filename'
            else:
                destrel=Path('notebooks')/name if len(rel.parts)==1 and p.suffix=='.ipynb' else (Path('src')/name if len(rel.parts)==1 and p.suffix=='.py' else rel)
                dest=target/destrel
                if dest.is_symlink() or any(parent.is_symlink() for parent in dest.parents if parent!=target):
                    rec['reason']='destination symlink'
                else:
                    try:
                        blob=cleaned_bytes(p)
                        rec.update({'destination':str(destrel),'sha256':hashlib.sha256(blob).hexdigest(),'bytes':len(blob)})
                        if dest.exists():rec['status']='identical' if dest.is_file() and dest.read_bytes()==blob else 'conflict'
                        elif str(destrel) in payloads:rec['status']='conflict';rec['reason']='multiple source files map to the same destination'
                        else:rec['status']='copy';payloads[str(destrel)]=blob
                    except (ValueError,UnicodeError,OSError) as e:
                        rec['status']='skip';rec['reason']=str(e)
            records.append(rec)
    return records,payloads

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--apply',action='store_true',help='Copy only reviewed new files; never overwrite conflicts.')
    args=parser.parse_args();source=args.source.expanduser().resolve();target=ROOT.resolve()
    if not source.is_dir():raise SystemExit('The source thesis directory does not exist.')
    if source==target or source in target.parents or target in source.parents:raise SystemExit('Use separate, non-overlapping source and destination directories.')
    records,payloads=plan(source,target)
    report=target/'publication-review.json';report.write_text(json.dumps({'applied':args.apply,'files':records},indent=2)+'\n')
    for status in ['copy','identical','conflict','skip']:print(f'{status}: {sum(r["status"]==status for r in records)}')
    print('Review publication-review.json. Possible secret values are never printed.')
    if args.apply:
        for rel,blob in payloads.items():
            dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True)
            # Exclusive creation refuses any file added since the review.
            with dest.open('xb') as f:f.write(blob)
        print('Copied new sources. Existing different files remain untouched. Nothing was staged, committed, or uploaded.')
    else:print('Dry run only. No thesis source was copied or changed.')

if __name__=='__main__':main()
