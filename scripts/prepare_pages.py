"""Inject the real GitHub repository URL into the static Pages payload."""
import json
import os
import re
from pathlib import Path

def prepare(repository, root):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
        raise SystemExit('Set GITHUB_REPOSITORY to the actual owner/repository.')
    config = {'repository_url': 'https://github.com/' + repository}
    (root/'docs/site-config.json').write_text(json.dumps(config)+'\n')
    print('Prepared the GitHub code links for the current repository.')

if __name__ == '__main__':
    prepare(os.environ.get('GITHUB_REPOSITORY'), Path(__file__).resolve().parents[1])
