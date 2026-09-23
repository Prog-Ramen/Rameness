import json, os, shlex, subprocess, sys
a = json.load(sys.stdin)
root = a.get("root", ".")
has = lambda f: os.path.exists(os.path.join(root, f))
if has("Cargo.toml"): cmd = ["cargo", "test"]
elif has("go.mod"): cmd = ["go", "test", "./..."]
elif has("package.json"): cmd = ["npm", "test", "--silent"]
elif has("pytest.ini") or has("conftest.py") or has("pyproject.toml"):
    cmd = [sys.executable, "-m", "pytest", "-q"]
else: cmd = [sys.executable, "-m", "unittest", "-q"]
cmd += shlex.split(a.get("args", ""))
p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=900)
print(json.dumps({"runner": " ".join(cmd), "passed": p.returncode == 0, "output": (p.stdout + p.stderr)[-6000:]}))
