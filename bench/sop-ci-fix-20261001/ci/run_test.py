"""Container-side runner. Assertions stay outside the untrusted SOP process."""
import json
import resource
import subprocess
import sys
import tempfile
from pathlib import Path

def run(entry, case):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.run([sys.executable, '-I', '-B', entry], input=json.dumps(case['input']).encode(),
            stdout=out, stderr=err, timeout=10,
            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536)))
        if p.returncode:
            raise ValueError(f'SOP exited {p.returncode}')
        out.seek(0)
        result = json.loads(out.read(65537))
    if not isinstance(result, dict):
        raise ValueError('SOP must emit a JSON object')
    for filename, expected in case.get('expect_files', {}).items():
        path = Path(filename)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('file assertions must use relative paths inside the test scratch directory')
        actual = json.loads(path.read_bytes()[:65537])
        if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError('written JSON file has incorrect content')
    return result

if __name__ == '__main__':
    try:
        json.dump(run(sys.argv[1], json.load(sys.stdin)), sys.stdout)
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
