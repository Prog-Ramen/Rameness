import json, os, sys, fnmatch
a = json.load(sys.stdin)
root, pat, limit = a.get("root", "."), a["pattern"], a.get("limit", 500)
skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".rameness", "dist", "build"}
out = []
for d, dirs, files in os.walk(root):
    dirs[:] = [x for x in dirs if x not in skip]
    for f in files:
        rel = os.path.relpath(os.path.join(d, f), root)
        if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(rel, pat):
            out.append(rel)
out.sort()
print(json.dumps({"files": out[:limit], "truncated": len(out) > limit}))
