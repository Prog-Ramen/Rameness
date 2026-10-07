"""Build Rameness format-3 sharded indexes without importing any SOP code."""
import hashlib
import json
from pathlib import Path

def build(root):
    root.mkdir(parents=True, exist_ok=True)
    for f in root.rglob('*index.json'):
        if f.name in {'index.json', '_index.json'}:
            f.unlink()
    def entry(path):
        sid = '.'.join(path.relative_to(root).parts)
        meta = path / 'sop.json'
        if meta.exists():
            m = json.loads(meta.read_text())
            files = [{'path': str(f.relative_to(root)), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()} for f in sorted(path.rglob('*')) if f.is_file() and '__pycache__' not in f.parts]
            return {'type': 'sop', 'id': sid, 'description': m['description'], 'keywords': m.get('keywords', []), 'kind': m['kind'], 'permissions': m.get('permissions', []), 'version': m.get('version', '1.0.0'), 'inputs': m['inputs'], 'status': m['status'], 'path': str(path.relative_to(root)), 'files': files}
        m = json.loads((path / '_node.json').read_text()) if (path / '_node.json').exists() else {}
        children = sorted(p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith('.'))
        return {'type': 'node', 'id': sid, 'description': m.get('description', sid), 'keywords': m.get('keywords', []), 'requires': m.get('requires', []), 'children': children}
    def walk(path):
        children = sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith('.'))
        entries = [entry(p) for p in children]
        for p in children:
            if not (p / 'sop.json').exists():
                walk(p)
        index = path / ('index.json' if path == root else '_index.json')
        index.write_text(json.dumps({'format': 3, 'id': '.'.join(path.relative_to(root).parts), 'entries': entries}, indent=1) + '\n')
    walk(root)

if __name__ == '__main__':
    build(Path('sops'))
