import json, sys, urllib.parse, urllib.request, urllib.error
a = json.load(sys.stdin)
if not a["url"].startswith(("http://", "https://")):
    sys.exit("invalid url")
headers = {"User-Agent": "rameness-sop/1", **a.get("headers", {})}
if "json" in a:
    data = json.dumps(a["json"]).encode(); headers.setdefault("Content-Type", "application/json")
else:
    data = urllib.parse.urlencode(a.get("form", {})).encode()
req = urllib.request.Request(a["url"], data=data, headers=headers, method="POST")
try:
    r = urllib.request.urlopen(req, timeout=30); status, body = r.status, r.read()
except urllib.error.HTTPError as e:
    status, body = e.code, e.read()
text = body.decode("utf-8", "replace")
try:
    print(json.dumps({"status": status, "json": json.loads(text)}))
except ValueError:
    print(json.dumps({"status": status, "body": text[:20000]}))
