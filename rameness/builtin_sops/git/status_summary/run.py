import json, subprocess, sys
a = json.load(sys.stdin)
p = subprocess.run(["git", "-C", a.get("repo", "."), "status", "--porcelain=v1", "-b"], capture_output=True, text=True)
if p.returncode:
    sys.exit(p.stderr)
lines = p.stdout.splitlines()
out = {"branch": lines[0][3:] if lines else "", "staged": [], "modified": [], "untracked": []}
for l in lines[1:]:
    x, y, f = l[0], l[1], l[3:]
    if x == "?":
        out["untracked"].append(f)
        continue
    if x != " ":
        out["staged"].append(f)
    if y != " ":
        out["modified"].append(f)
print(json.dumps(out))
