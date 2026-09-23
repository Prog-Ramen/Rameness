import csv, json, sys
a = json.load(sys.stdin)
with open(a["file"], newline="") as f:
    rows = list(csv.DictReader(f, delimiter=a.get("delimiter", ",")))
cols = list(rows[0].keys()) if rows else []
num = {}
for c in cols:
    try:
        vals = [float(r[c]) for r in rows if r[c] not in ("", None)]
    except ValueError:
        continue
    if vals:
        num[c] = {"min": min(vals), "max": max(vals), "mean": sum(vals) / len(vals)}
print(json.dumps({"rows": len(rows), "columns": cols, "numeric": num}))
