"""Runs only from main. Reads PR blobs, never imports or executes their code."""
import json
import base64
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from review import markdown, review
from semantic import decide, policy_id
from security import assess, close_and_cleanup

REPO = os.environ.get('GH_REPO', '')
TOKEN = os.environ.get('GH_TOKEN', '')
MARKER = '<!-- ramensops-ci-review -->'

def api(path, data=None, method=None):
    req = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None

def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, check=True, timeout=60).stdout.strip()

def checked(number, sha):
    # Independently verify both named workflows completed successfully for this exact PR SHA.
    # Do not trust reports/artifacts, and do not trust similarly named checks from other apps.
    for filename in ['sop-check.yml', 'secret-scan.yml']:
        runs = api(f'actions/workflows/{filename}/runs?head_sha={sha}&per_page=100')['workflow_runs']
        candidates = [r for r in runs if r['head_sha'] == sha and r['event'] == 'pull_request'
                      and r['path'] == '.github/workflows/' + filename
                      and r['head_repository']['full_name'] == api(f'pulls/{number}')['head']['repo']['full_name']]
        # Manual reruns use main as head_sha; their results cannot substitute for exact-head PR checks.
        if not candidates or max(candidates, key=lambda r: r['id'])['conclusion'] != 'success':
            return False
    return True

LABELS = {'eligible': ('sop:auto-merge', '0e8a16'), 'human': ('sop:needs-human', 'd93f0b'),
          'checking': ('sop:checking', 'fbca04'), 'reviewer-error': ('sop:reviewer-error', 'b60205'),
          'failed': ('sop:checks-failed', 'b60205'), 'security': ('sop:security-rejected', 'b60205')}

def label_for(result):
    if result.get('security', {}).get('verdict') == 'prohibited':
        return LABELS['security'][0]
    if not result['ok']:
        return LABELS['failed'][0]
    if result['human']:
        return LABELS['human'][0]
    if result.get('semantic', {}).get('unavailable'):
        return LABELS['reviewer-error'][0]
    if result.get('waiting'):
        return LABELS['checking'][0]
    return LABELS['eligible'][0] if result['auto_merge'] else LABELS['human'][0]

def report(number, result):
    label = label_for(result)
    for name, color in LABELS.values():
        try:
            api('labels', {'name': name, 'color': color})
        except urllib.error.HTTPError as e:
            if e.code != 422:
                raise
    api(f'issues/{number}/labels', {'labels': [label]})
    for other, _ in LABELS.values():
        if other == label:
            continue
        try:
            api(f'issues/{number}/labels/{other}', method='DELETE')
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    body = MARKER + '\n' + markdown(result)
    if result.get('semantic') and not result['semantic'].get('unavailable'):
        cache = {'head': result['head'], 'policy': policy_id(), 'judgment': result['semantic']}
        body += '\n<!-- sop-semantic-cache:' + base64.b64encode(json.dumps(cache).encode()).decode() + ' -->'

    comments = api(f'issues/{number}/comments?per_page=100')
    previous = next((c for c in reversed(comments) if c['user']['login'] == 'github-actions[bot]' and c['body'].startswith(MARKER)), None)
    if previous:
        if previous['body'] != body:
            api(f"issues/comments/{previous['id']}", {'body': body}, 'PATCH')
    else:
        api(f'issues/{number}/comments', {'body': body})

def cached_semantic(number, sha):
    for c in reversed(api(f'issues/{number}/comments?per_page=100')):
        if c['user']['login'] != 'github-actions[bot]' or not c['body'].startswith(MARKER):
            continue
        try:
            raw = c['body'].split('<!-- sop-semantic-cache:', 1)[1].split(' -->', 1)[0]
            cache = json.loads(base64.b64decode(raw, validate=True))
            if cache['head'] == sha and cache['policy'] == policy_id() and type(cache['judgment']['approved']) is bool:
                return cache['judgment']
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    return None

def main():
    merged = False
    # Pagination is bounded to 200 PRs to keep the job's resource use predictable.
    pulls = []
    for page in (1, 2):
        batch = api(f'pulls?state=open&base=main&per_page=100&page={page}')
        pulls.extend(batch)
        if len(batch) < 100:
            break
    for item in pulls:
        number = item['number']
        pr = api(f'pulls/{number}')
        if not pr['head']['repo']:
            continue
        sha = pr['head']['sha']
        git('fetch', '--no-tags', 'origin', 'main')
        git('fetch', '--no-tags', 'origin', f'refs/pull/{number}/head')
        if git('rev-parse', 'FETCH_HEAD') != sha:
            continue
        base = git('rev-parse', 'origin/main')
        security = assess(Path('.'), base, sha, TOKEN)
        if security['verdict'] == 'prohibited':
            result = {'ok': False, 'auto_merge': False, 'head': sha, 'fail': [], 'human': [], 'sops': [], 'security': security}
            result['remediation'] = close_and_cleanup(api, REPO, TOKEN, number, sha, security)
            report(number, result)
            continue
        result = review(Path('.'), base, sha, execute=False)
        result['security'] = security
        if security['verdict'] == 'unavailable':
            result['auto_merge'] = False
            result['semantic'] = {'approved': False, 'unavailable': True, 'reason': 'Security review unavailable; automatic merge is blocked.'}
        elif security['verdict'] == 'uncertain':
            result['auto_merge'] = False
            result['human'].append('Security review is uncertain; maintainer review required.')
        if pr['draft']:
            continue
        # Require a current branch; a main change cannot reuse old successful tests.
        up_to_date = subprocess.run(['git', 'merge-base', '--is-ancestor', base, sha], capture_output=True).returncode == 0
        if not up_to_date:
            result.setdefault('waiting', []).append('branch must be updated with current main before automatic merging')
            result['auto_merge'] = False
        ready = result['auto_merge'] and checked(number, sha)
        if ready:
            result['semantic'] = cached_semantic(number, sha) or decide(Path('.'), base, sha, result['sops'], TOKEN)
            if not result['semantic']['approved']:
                if not result['semantic'].get('unavailable'):
                    result['human'].append(result['semantic']['reason'])
                result['auto_merge'] = False
        elif result['auto_merge']:
            result.setdefault('waiting', []).append('waiting for successful sop-check and secret-scan for this exact commit')
            result['auto_merge'] = False
        report(number, result)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as f:
                f.write(f'### PR #{number}\n' + markdown(result))
        if not result['auto_merge'] or not ready:
            print(f'PR #{number}: waiting for checks or maintainer review')
            continue
        # Re-read immediately before merge. The merge API's sha is an atomic head-commit guard.
        fresh = api(f'pulls/{number}')
        if fresh['head']['sha'] != sha or fresh['state'] != 'open' or fresh['draft']:
            continue
        if api('git/ref/heads/main')['object']['sha'] != base:
            continue
        try:
            response = api(f'pulls/{number}/merge', {'sha': sha, 'merge_method': 'squash'}, 'PUT')
        except urllib.error.HTTPError as e:
            if e.code in (405, 409, 422):
                print(f'PR #{number}: GitHub merge requirements not yet met ({e.code})')
                continue
            raise
        if response.get('merged'):
            print(f'PR #{number}: merged checked commit {sha}')
            merged = True
    if merged:
        api('actions/workflows/registry-index.yml/dispatches', {'ref': 'main'})

if __name__ == '__main__':
    main()
