import json, sys, urllib.parse, urllib.request, urllib.error
a = json.load(sys.stdin)
url = a["url"]
if a.get("params"):
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(a["params"])
if not url.startswith(("http://", "https://")):
    sys.exit(f"invalid url: {url}")
req = urllib.request.Request(url, headers={"User-Agent": "rameness-sop/1", **a.get("headers", {})})
try:
    r = urllib.request.urlopen(req, timeout=30)
    status, body, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
except urllib.error.HTTPError as e:
    status, body, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
text = body.decode("utf-8", "replace")
out = {"status": status, "content_type": ctype}
try:
    out["json"] = json.loads(text)
except ValueError:
    out["body"] = text[: a.get("max_chars", 20000)]
print(json.dumps(out))
