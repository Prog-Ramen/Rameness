import json, sys
a = json.load(sys.stdin)
if "data" in a:
    doc = a["data"]
elif "file" in a:
    doc = json.load(open(a["file"]))
else:
    doc = json.loads(a["text"])

def get(cur, parts):
    if not parts:
        return cur
    p, rest = parts[0], parts[1:]
    if p == "*":
        return [get(x, rest) for x in (cur if isinstance(cur, list) else cur.values())]
    if isinstance(cur, list):
        return get(cur[int(p)], rest)
    return get(cur[p], rest) if isinstance(cur, dict) and p in cur else None

print(json.dumps({"values": {p: get(doc, p.split(".")) for p in a["paths"]}}))
