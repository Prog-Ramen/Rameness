"""Trusted, dependency-free SOP gate. PR files are data; execution happens only in Docker."""
from __future__ import annotations
import argparse
import ast
import json
import re
import subprocess
import tempfile
import uuid
import resource
from pathlib import Path, PurePosixPath

IMAGE = "python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
PERMISSIONS = {"compute", "fs:read", "fs:write", "network", "exec", "side-effect"}
AUTO_PERMISSIONS = {"compute", "fs:read", "fs:write"}
MODULES = set("json sys re math statistics collections itertools functools operator decimal fractions datetime calendar string unicodedata csv io hashlib base64 binascii struct textwrap typing enum dataclasses pathlib".split())
PRIVATE = re.compile(r"(?:/home/[^/\s]+/|/Users/[^/\s]+/|/root/|[\w.-]+\.(?:internal|local|corp)\b|\b(?:organization|company|customer)[ -]specific\b|\b(?:PRIVATE_ONLY|CEDARDOCK_ONLY)\b)", re.I)
SECRET = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})\b")

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=30).stdout

def blob(repo, ref, path):
    data = git(repo, "show", f"{ref}:{path}")
    if len(data) > 256_000:
        raise ValueError(f"{path}: file exceeds 256 KB")
    return data

def tree(repo, ref):
    result = {}
    for item in git(repo, "ls-tree", "-r", "-z", ref).split(b"\0"):
        if not item:
            continue
        info, raw_path = item.split(b"\t", 1)
        mode, kind, sha = info.decode().split()
        path = raw_path.decode("utf-8")
        result[path] = (mode, kind, sha)
    return result

def source_checks(code, permissions):
    fail, human = [], []
    try:
        parsed = ast.parse(code)
    except SyntaxError:
        return ["invalid Python syntax"], []
    nodes = list(ast.walk(parsed))
    imports = {alias.name.split('.')[0] for n in nodes if isinstance(n, ast.Import) for alias in n.names}
    imports |= {n.module.split('.')[0] for n in nodes if isinstance(n, ast.ImportFrom) and n.module}
    aliases = {a.asname or a.name: a.name for n in nodes if isinstance(n, ast.Import) for a in n.names}
    for n in nodes:
        if isinstance(n, ast.ImportFrom) and n.module == 'sys' and any(a.name not in {'stdin','stdout','stderr'} for a in n.names):
            human.append('import from process internals')
    extra = imports - MODULES
    if extra:
        human.append(f"imports outside the automatic-review standard-library set: {sorted(extra)}")
    visible = set()
    if imports & {"socket", "requests", "httpx", "urllib", "http", "aiohttp"}:
        visible.add("network")
    if imports & {"subprocess", "pty"}:
        visible.add("exec")
    for n in nodes:
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in {'eval','exec','compile','__import__','getattr','setattr','globals','locals','vars','breakpoint'}:
            human.append('dynamic execution or introspection reference')
        if isinstance(n, ast.Attribute):
            if n.attr.startswith('__') or (isinstance(n.value, ast.Name) and aliases.get(n.value.id, n.value.id) == 'sys' and n.attr not in {'stdin', 'stdout', 'stderr'}):
                human.append("introspection or access to process internals")
            if n.attr in {'read_text', 'read_bytes', 'iterdir', 'glob', 'rglob', 'stat', 'lstat'}:
                visible.add('fs:read')
            if n.attr in {"write_text", "write_bytes", "mkdir", "unlink", "rename", "replace", "rmdir", "touch"}:
                visible.add("fs:write")
            if n.attr in {"system", "popen", "spawn", "execv", "execve"}:
                visible.add("exec")
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id in {"eval", "exec", "compile", "__import__", "getattr", "setattr", "globals", "locals", "vars", "breakpoint"}:
                human.append("dynamic execution or introspection")
            if n.func.id == 'open':
                mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == 'mode'), ast.Constant('r'))
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                    visible.add('fs:write' if any(c in mode.value for c in 'wax+') else 'fs:read')
                else:
                    human.append("dynamic file access mode")
        if isinstance(n, ast.ImportFrom) and (n.level or any(a.name == '*' for a in n.names)):
            human.append("relative or wildcard import")
    if visible - permissions:
        fail.append(f"undeclared permissions: {sorted(visible - permissions)}")
    if not re.search(r'json\.(?:load|loads)\(', code) or not re.search(r'json\.(?:dump|dumps)\(', code):
        fail.append("entry must read JSON input and emit JSON output")
    return fail, sorted(set(human))

def criteria(meta, directory, files):
    fail, human = [], []
    sid = directory.removeprefix('sops/').replace('/', '.')
    if not isinstance(meta, dict):
        return ["metadata must be an object"], [], []
    if meta.get('id') != sid or not re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+', sid):
        fail.append("id must match its category/name path")
    description = meta.get('description')
    if not isinstance(description, str) or len(description.split()) < 12:
        fail.append("description must explain the reusable operation, inputs and result (at least 12 words)")
    if meta.get('scope', 'public') != 'public' or meta.get('status') != 'validated':
        fail.append("only validated public SOPs can be published")
    if not isinstance(meta.get('version'), str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', meta['version']):
        fail.append('version must use major.minor.patch numbers')
    if meta.get('kind') != 'script':
        human.append("skills and composites require maintainer review")
    permissions = meta.get('permissions')
    if not isinstance(permissions, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions):
        fail.append("permissions must be an array of known permission names")
        permissions = []
    if set(permissions) - AUTO_PERMISSIONS:
        human.append(f"powerful permissions: {sorted(set(permissions) - AUTO_PERMISSIONS)}")
    inputs = meta.get('inputs', {})
    props = inputs.get('properties', {}) if isinstance(inputs, dict) else {}
    if not isinstance(inputs, dict) or inputs.get('type') != 'object' or not isinstance(props, dict) or not props:
        fail.append("a reusable SOP must declare a parameterized object input schema")
        props = {}
    required = inputs.get('required', []) if isinstance(inputs, dict) else []
    if not isinstance(required, list) or any(not isinstance(k, str) or k not in props for k in required):
        fail.append("required inputs must name declared properties")
        required = []
    entry = meta.get('entry', 'run.py')
    if not isinstance(entry, str) or entry != PurePosixPath(entry).name or entry not in files:
        fail.append("entry must name a file directly inside the SOP directory")
    elif not entry.endswith('.py'):
        human.append("only Python scripts are automatically reviewed")
    for path, data in files.items():
        text = data.decode('utf-8', errors='replace')
        if SECRET.search(text):
            fail.append(f"{path}: possible secret")
        if PRIVATE.search(text):
            human.append(f"{path}: possibly private or organization-specific data")
        if path.endswith('.py'):
            f, h = source_checks(text, set(permissions))
            # Helpers need not implement the JSON entry-point protocol.
            if path != entry:
                f = [x for x in f if not x.startswith('entry must')]
            fail.extend(f'{path}: {x}' for x in f)
            human.extend(f'{path}: {x}' for x in h)
        elif path != 'sop.json' and not path.endswith(('.md', '.txt', '.json', '.csv')):
            human.append(f"{path}: file type needs maintainer review")
    tests = meta.get('tests', [])
    if not isinstance(tests, list) or not 2 <= len(tests) <= 40:
        fail.append("provide between 2 and 40 concrete tests, including a normal and an edge case")
        tests = []
    valid = []
    types = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool, 'array': list, 'object': dict, 'null': type(None)}
    for i, t in enumerate(tests):
        if not isinstance(t, dict) or not isinstance(t.get('input'), dict) or not isinstance(t.get('expect'), dict) or not t['expect'] or t.get('expect_error'):
            fail.append(f"test {i}: requires object input and concrete nonempty expected values; key-only/error-only tests do not qualify")
            continue
        if any(k not in t['input'] for k in required):
            fail.append(f"test {i}: missing required input")
        for k, v in t['input'].items():
            if k not in props:
                fail.append(f'test {i}: undeclared input {k}')
            declared = props.get(k, {}).get('type') if isinstance(props.get(k), dict) else None
            if declared in types and (not isinstance(v, types[declared]) or declared in {'integer', 'number'} and isinstance(v, bool)):
                fail.append(f"test {i}: wrong type for {k}")
        assertions = t.get('expect_files', {})
        if not isinstance(assertions, dict) or any(not isinstance(k, str) or PurePosixPath(k).is_absolute() or '..' in PurePosixPath(k).parts for k in assertions):
            fail.append(f'test {i}: file assertions must use safe relative paths')
        valid.append(t)
    if valid and len({json.dumps(t['input'], sort_keys=True) for t in valid}) < 2:
        fail.append("tests must exercise distinct inputs")
    if valid and len({json.dumps(t['expect'], sort_keys=True) for t in valid}) < 2:
        human.append("all test results are identical; meaningful behavior needs maintainer review")
    return fail, sorted(set(human)), valid

def run_case(directory, entry, case):
    # No token, network, host write mount, privileged capabilities or Docker socket.
    name = 'ramensops-test-' + uuid.uuid4().hex
    cmd = ['docker', 'run', '-i', '--name', name, '--rm', '--network=none', '--read-only', '--cap-drop=ALL',
           '--security-opt=no-new-privileges', '--pids-limit=32', '--memory=256m', '--cpus=1',
           '--user=65534:65534', '--tmpfs=/tmp:rw,noexec,nosuid,size=32m,mode=1777',
           '--mount', f'type=bind,src={directory},dst=/sop,readonly', '--workdir=/tmp',
           '--mount', f'type=bind,src={Path(__file__).with_name("run_test.py").resolve()},dst=/runner.py,readonly',
           IMAGE, 'python', '-I', '-B', '/runner.py', f'/sop/{entry}']
    # Use files rather than capture_output: malicious output cannot exhaust host memory.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            p = subprocess.run(cmd, input=json.dumps(case).encode(), stdout=out, stderr=err, timeout=15,
                               preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (1_048_576, 1_048_576)))
        except subprocess.TimeoutExpired:
            raise ValueError('test exceeded 15 seconds')
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)
        if p.returncode:
            err.seek(0)
            diagnostic = err.read(512).decode('utf-8', errors='replace')
            raise ValueError(f'test exited {p.returncode}: {diagnostic!r}')
        out.seek(0)
        data = out.read(65537)
        if len(data) > 65536:
            raise ValueError('test output exceeds 64 KB')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('script must emit a JSON object')
        for key, expected in case['expect'].items():
            got = result
            try:
                for part in key.split('.'):
                    got = got[int(part)] if isinstance(got, list) else got[part]
            except (KeyError, IndexError, ValueError, TypeError):
                raise ValueError(f'missing expected output {key}')
            if json.dumps(got, sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise ValueError(f'wrong value for {key}')
        for key in case.get('expect_keys', []):
            if key not in result:
                raise ValueError(f'missing output key {key}')

def review(repo, base, head, execute=False, runner=run_case):
    fail, human, sops = [], [], []
    base = git(repo, 'rev-parse', '--verify', base + '^{commit}').decode().strip()
    head = git(repo, 'rev-parse', '--verify', head + '^{commit}').decode().strip()
    before, after = tree(repo, base), tree(repo, head)
    # Compare full snapshots, so an SOP PR cannot hide a workflow change in an intermediate commit.
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if not changed:
        fail.append('no changes')
    if len(changed) > 100:
        fail.append('too many changed files (maximum 100)')
    if any(not p.startswith('sops/') for p in changed):
        human.append('changes outside sops/: maintenance PR requires maintainer review')
    dirs = set()
    for p in changed:
        if not p.startswith('sops/'):
            continue
        if PurePosixPath(p).name in {'index.json', '_index.json'}:
            fail.append(f'{p}: generated indexes must not be committed')
            continue
        if p not in after:
            human.append(f'{p}: file deletion requires maintainer review')
            continue
        parent = PurePosixPath(p).parent
        while str(parent) != 'sops' and str(parent / 'sop.json') not in after:
            parent = parent.parent
        if str(parent) == 'sops':
            fail.append(f'{p}: no owning sop.json')
        else:
            dirs.add(str(parent))
    if len(dirs) > 10:
        fail.append('maximum 10 changed SOPs per PR')
        dirs = set()
    for d in sorted(dirs):
        local_fail, local_human = [], []
        try:
            paths = [p for p in after if p.startswith(d + '/')]
            if len(paths) > 32:
                raise ValueError('maximum 32 files per SOP')
            files = {}
            for p in paths:
                name = p[len(d)+1:]
                if after[p][0] not in {'100644', '100755'} or any(x in {'..', '.git'} for x in PurePosixPath(name).parts):
                    raise ValueError('symlinks, submodules and unsafe paths are forbidden')
                files[name] = blob(repo, head, p)
            if sum(map(len, files.values())) > 1_000_000:
                raise ValueError('SOP exceeds 1 MB')
            meta = json.loads(files['sop.json'])
            local_fail, local_human, tests = criteria(meta, d, files)
            previous = before.get(d + '/sop.json')
            regressions = []
            if previous:
                old = json.loads(blob(repo, base, d + '/sop.json'))
                if old.get('inputs') != meta.get('inputs') or old.get('permissions') != meta.get('permissions') or old.get('kind') != meta.get('kind'):
                    local_human.append('input schema, permissions or kind changed; compatibility requires maintainer review')
                regressions = old.get('tests', [])
                # Preserve old assertions, not merely successful execution on their input.
                for t in regressions:
                    if t not in tests:
                        local_fail.append('existing tests must be retained unchanged alongside new tests')
                if old.get('version') == meta.get('version'):
                    local_fail.append('an update must change its version')
            if execute and not local_fail and not local_human:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    root.chmod(0o755)
                    for name, data in files.items():
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                        path.chmod(0o644)
                    for i, case in enumerate(tests):
                        try:
                            runner(root, meta.get('entry', 'run.py'), case)
                        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
                            local_fail.append(f'test {i}: {e}')
            sops.append({'id': meta.get('id'), 'new': not bool(previous), 'tests': len(tests), 'regression_tests': len(regressions)})
        except (ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as e:
            local_fail.append(str(e))
        fail.extend(f'{d}: {x}' for x in local_fail)
        human.extend(f'{d}: {x}' for x in local_human)
    return {'ok': not fail, 'auto_merge': not fail and not human and bool(sops), 'fail': fail, 'human': human, 'sops': sops, 'base': base, 'head': head, 'tests_executed': execute}

def markdown(result):
    verdict = ('Security rejection — proposal closed' if result.get('security', {}).get('verdict') == 'prohibited' and result.get('remediation', {}).get('closed')
               else 'Security rejection — cleanup pending' if result.get('security', {}).get('verdict') == 'prohibited'
               else 'Checks failed' if not result['ok'] else 'Maintainer review required' if result['human']
               else 'Reviewer error — automatic retry pending' if result.get('semantic', {}).get('unavailable')
               else 'Waiting for CI or branch update' if result.get('waiting')
               else 'Eligible for automatic merge' if result['auto_merge'] else 'Maintainer review required')
    lines = ['## SOP review: ' + verdict, '', f"Reviewed commit: `{result['head']}`", '']
    if result.get('security', {}).get('verdict') == 'prohibited':
        categories = sorted({f['category'] for f in result['security']['findings']})
        lines.append('Rejected categories: ' + ', '.join(categories) + '.')
        lines.append('Cleanup: ' + result.get('remediation', {}).get('cleanup', 'pending') + '.')
        lines.append('Closing and branch deletion do not erase cached commits, PR references or forks. Credential revocation and GitHub support/history cleanup may still be required.')
        lines.append('')
        return '\n'.join(lines) + '\n'
    if result.get('semantic'):
        lines.append('Semantic review: ' + str(result['semantic']['reason']).replace('\n', ' ')[:1200])
        lines.append('')
    for sop in result['sops']:
        lines.append(f"- `{sop['id']}`: {sop['tests']} concrete tests; {sop['regression_tests']} retained regression tests.")
    for label, key in [('Must fix', 'fail'), ('Needs human review', 'human'), ('Waiting for automation', 'waiting')]:
        if result.get(key):
            lines.extend(['', '**' + label + ':**'])
            lines.extend('- ' + str(x).replace('\n', ' ')[:500] for x in result[key])
    return '\n'.join(lines) + '\n'

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', type=Path, default=Path('.'))
    ap.add_argument('--base', required=True)
    ap.add_argument('--head', required=True)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--json', type=Path, required=True)
    ap.add_argument('--markdown', type=Path, required=True)
    args = ap.parse_args()
    r = review(args.repo, args.base, args.head, args.execute)
    args.json.write_text(json.dumps(r, indent=2) + '\n')
    args.markdown.write_text(markdown(r))
    print(markdown(r))
    raise SystemExit(0 if r['ok'] else 1)
