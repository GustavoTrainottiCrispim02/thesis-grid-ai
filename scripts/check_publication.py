"""Check a curated publication tree without printing possible secret values.

This catches common credential formats and obvious excluded files. It is not
a complete security audit; manually review the exact staged diff before upload.
"""
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b'),
    re.compile(r'https?://[^\s/]+:[^\s/]+@'),
]
BLOCKED = {'.env','.openai','.venv','venv','node_modules','.DS_Store','__pycache__','.ipynb_checkpoints'}

def violations(text):
    return any(pattern.search(text) for pattern in PATTERNS)

def check(root=ROOT):
    issues=[];count=0
    for p in sorted(root.rglob('*')):
        rel=p.relative_to(root)
        if '.git' in rel.parts: continue
        if p.is_symlink(): issues.append((str(rel),'symlink'));continue
        if not p.is_file():continue
        count+=1
        if any(part in BLOCKED or part.startswith('.env.') for part in rel.parts):
            issues.append((str(rel),'excluded workstation or credential file'))
        if p.stat().st_size > 50*1024*1024: issues.append((str(rel),'large file; review separately'))
        if p.suffix=='.zip':
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if info.is_dir():continue
                    if info.file_size > 50*1024*1024:issues.append((str(rel),'large archive member'));continue
                    raw=z.read(info).decode('utf-8',errors='ignore')
                    if violations(raw):issues.append((str(rel)+' :: '+info.filename,'possible credential signature'))
        else:
            text=p.read_text(errors='ignore')
            if violations(text):issues.append((str(rel),'possible credential signature'))
            if p.suffix=='.ipynb':
                nb=json.loads(text)
                if any(c.get('outputs') or c.get('execution_count') is not None for c in nb['cells'] if c['cell_type']=='code'):
                    issues.append((str(rel),'notebook execution output is present'))
    if issues:
        for path,reason in issues:print(f'REVIEW: {path}: {reason}')
        raise SystemExit('Publication check requires review. No possible secret values were printed.')
    print(f'PASS: {count} curated files; notebooks have no execution output; no common credential signatures detected.')

if __name__=='__main__':check()
