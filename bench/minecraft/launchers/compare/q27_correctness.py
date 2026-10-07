"""Gibberish hunt for the 27B + DFlash2 stack: same prompts and seeds against one running server.

    python3 q27_correctness.py LABEL http://127.0.0.1:8000 MODEL >> q27-correctness.jsonl

Each prompt runs at temperature 0 (compared exactly against the no-speculation reference later) and at
temperature 1.0 with fixed seeds (the way Rameness samples). Every output is scored for the failure seen in the
Minecraft run: stray non-ASCII characters in code, JavaScript that no longer parses, and file paths that come back
mangled when the model copies them from its context.
"""
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

LABEL, URL, MODEL = sys.argv[1], sys.argv[2].rstrip("/") + "/v1/chat/completions", sys.argv[3]
PATH = "/home/bench/runs/mc-q27gptq-df15-2-rameness-q27gptq/work/js/mesher.js"
FILE = open(__file__.rsplit("/", 1)[0] + "/q27_sample_mesher.js").read()

PROMPTS = {
    "write_js": ("Write a complete JavaScript ES module `mesher.js` for a voxel game: export a function "
                 "meshChunk(blocks, size) that returns {positions, normals, uvs, indices} for all visible cube faces "
                 "(skip faces between two solid blocks). Include a FACES table with the six face normals and corners. "
                 "Reply with only the code, no explanation."),
    "edit_in_context": (f"Here is the file {PATH}:\n\n```js\n{FILE}\n```\n\nRewrite the whole file with one change: rename "
                        "the function `buildChunkMesh` to `meshChunkGeometry` everywhere. Reply with only the complete updated "
                        "file in a ```js block."),
    "paths": (f"The project lives in {PATH.rsplit('/js/', 1)[0]}. List the full absolute paths of these files, one per "
              "line, nothing else: js/mesher.js, js/blocks.js, js/world.js, js/player.js, js/main.js, index.html, "
              "css/style.css, tests/e2e.mjs"),
}
EXPECTED_PATHS = [PATH.rsplit("/js/", 1)[0] + "/" + p for p in
                  ("js/mesher.js", "js/blocks.js", "js/world.js", "js/player.js", "js/main.js", "index.html",
                   "css/style.css", "tests/e2e.mjs")]


def ask(prompt, temperature, seed):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1800,
            "temperature": temperature, "top_p": 0.95 if temperature else 1.0, "seed": seed,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        out = json.loads(r.read())
    msg = out["choices"][0]["message"]
    return (msg.get("content") or ""), out.get("usage", {}).get("completion_tokens"), time.time() - t


def js_parses(code):
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(code)
    return subprocess.run(["node", "--check", f.name], capture_output=True).returncode == 0


def score(name, text):
    code = re.findall(r"```(?:js|javascript)?\n(.*?)```", text, re.S)
    code = code[0] if code else text
    s = {"non_ascii": len(re.findall(r"[^\x00-\x7F]", code))}
    if name in ("write_js", "edit_in_context"):
        s["js_parses"] = js_parses(code)
    if name == "edit_in_context":
        s["renamed"] = "meshChunkGeometry" in code and "buildChunkMesh" not in code
    if name == "paths":
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        s["paths_exact"] = sum(1 for p in EXPECTED_PATHS if p in lines)
        s["paths_mangled"] = [l for l in lines if l.startswith("/home/bench/runs/") and l not in EXPECTED_PATHS]
    return s


for name, prompt in PROMPTS.items():
    for temperature, seed in [(0.0, 0), (1.0, 1), (1.0, 2), (1.0, 3)]:
        try:
            text, ntok, secs = ask(prompt, temperature, seed)
            row = {"label": LABEL, "prompt": name, "temperature": temperature, "seed": seed, "tokens": ntok,
                   "tps": round((ntok or 0) / max(secs, 1e-6), 1), **score(name, text), "text": text}
        except Exception as e:
            row = {"label": LABEL, "prompt": name, "temperature": temperature, "seed": seed, "error": str(e)[:300]}
        print(json.dumps(row), flush=True)
