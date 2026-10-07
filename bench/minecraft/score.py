"""Score a Minecraft build black-box: load it in headless Chromium and check it behaves like one.

    /root/bench/venv/bin/python -m bench.minecraft.score <workdir> [--screenshot out.png]

The task prompt is minimal, so builds expose no test API. Each behaviour check compares
screenshots around an input against the scene's own idle change over the same time (animated
water or clouds are not a response): walking, looking, jumping, breaking and placing blocks
(change around the crosshair), number keys changing the UI. Prints one JSON object: every check
with pass/fail and detail, the score (percent passed), FPS, page errors and code size. If a
build happens to expose ``window.game`` (camera / getBlock / blockCount), those facts are
recorded as extra information, not scored. Rendering uses SwiftShader (software WebGL), so FPS
compares runs with each other, not with a real GPU.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import json
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

CHECKS = ["index_html", "page_loads", "no_page_errors", "clean_console", "webgl_canvas", "renders_scene",
          "has_ui_overlay", "walks", "mouse_look", "jumps", "breaks_block", "places_block", "number_keys",
          "fps_15"]

# Headless Chromium never grants pointer lock, and most games only take mouse input while locked.
# Emulate a granted lock so games are tested on their game logic, not on the browser.
POINTER_LOCK_SHIM = """
(() => { let el = null; const fire = () => document.dispatchEvent(new Event('pointerlockchange'));
  Object.defineProperty(Document.prototype, 'pointerLockElement', {configurable: true, get() { return el; }});
  Element.prototype.requestPointerLock = function () { el = this; setTimeout(fire, 0); return Promise.resolve(); };
  Document.prototype.exitPointerLock = function () { el = null; setTimeout(fire, 0); };
})();
"""
W, H = 1280, 800


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    h.log_message = lambda *a: None
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, s.server_address[1]


def code_stats(root: Path) -> dict:
    files = [f for f in root.rglob("*") if f.is_file() and f.suffix in (".html", ".js", ".css", ".mjs")
             and not any(p.startswith(".") or p == "node_modules" for p in f.relative_to(root).parts)]
    loc = sum(len(f.read_text(errors="replace").splitlines()) for f in files)
    return {"files": len(files), "loc": loc, "bytes": sum(f.stat().st_size for f in files)}


class Shots:
    """Screenshots reduced to 320x200 and compared as the fraction of pixels that changed."""

    def __init__(self, page):
        self.page = page

    def grab(self) -> Image.Image:
        return Image.open(io.BytesIO(self.page.screenshot())).convert("RGB").resize((320, 200))

    @staticmethod
    def diff(a: Image.Image, b: Image.Image, box=None) -> float:
        if box:
            a, b = a.crop(box), b.crop(box)
        d = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
        return d.histogram()[255] / (d.width * d.height)


CENTER = (128, 70, 192, 130)            # the crosshair area of a 320x200 frame


def score(root: Path, shot: Path | None = None) -> dict:
    res = {c: {"pass": False, "detail": ""} for c in CHECKS}
    out = {"checks": res, "fps": None, "page_errors": [], "console_errors": 0, "api": None, **code_stats(root)}

    def ok(name, cond, detail=""):
        res[name] = {"pass": bool(cond), "detail": str(detail)[:300]}

    ok("index_html", (root / "index.html").exists())
    if not res["index_html"]["pass"]:
        return finish(out)
    srv, port = serve(root)
    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="chromium", headless=True, args=[
            "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
            "--enable-webgl", f"--window-size={W},{H}"])
        page = br.new_page(viewport={"width": W, "height": H})
        page.add_init_script(POINTER_LOCK_SHIM)
        page.on("pageerror", lambda e: out["page_errors"].append(str(e)[:300]))
        def console(m):                                  # the browser's own favicon 404 is not the game's error
            if m.type == "error" and "favicon" not in (m.location or {}).get("url", "") + m.text:
                out["console_errors"] += 1
                out.setdefault("console_samples", []).append(m.text[:200])
        page.on("console", console)
        r = None
        try:
            r = page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load", timeout=60000)
            ok("page_loads", r is not None and r.ok, r.status if r else "no response")
        except Exception as e:
            ok("page_loads", False, e)
        page.wait_for_timeout(8000)                      # world generation, first frames
        s = Shots(page)
        try:
            behaviour(page, s, ok, out, res, r, shot)
        except Exception as e:
            out["page_errors"].append(f"scorer: {e}"[:300])
        br.close()
    srv.shutdown()
    return finish(out)


def behaviour(page, s: Shots, ok, out, res, r, shot):
    ev = page.evaluate
    gl = ev("""() => { for (const c of document.querySelectorAll('canvas')) {
        const g = c.getContext('webgl2') || c.getContext('webgl'); if (g) return c.width + 'x' + c.height; }
        return null; }""")
    ok("webgl_canvas", gl, gl)
    # many builds open on a title screen: press its Play / Start button if there is one, else click the
    # middle once ("click to play"), as a player would
    start = ev("""() => {
        // a start control first (Play, Start, "click to play"...); world options (New world...) only if there is none
        const tiers = [/^[^a-z0-9]*(play|start|start game|play game|begin|enter world|enter game|launch|click to (play|start|begin)|resume)\\b/i,
                       /^[^a-z0-9]*(new world|new game|create world|generate world)\\b/i];
        const els = [...document.querySelectorAll('button, [role=button], a, input[type=button], input[type=submit], h1, h2, h3, p, div, span')]
            .filter(e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
                return r.width > 4 && r.height > 4 && st.visibility !== 'hidden' && st.display !== 'none'
                       && +st.opacity > 0.05 && (e.innerText || e.value || '').trim().length < 40; });
        for (const want of tiers) {
            const hits = els.filter(e => want.test((e.innerText || e.value || '').trim()));
            // the innermost match (a <button>, not the dialog that contains it)
            const hit = hits.find(e => !hits.some(o => o !== e && e.contains(o)));
            if (hit) { const r = hit.getBoundingClientRect();
                       return [r.x + r.width / 2, r.y + r.height / 2, (hit.innerText || hit.value).trim()]; }
        }
        return null; }""")
    if start:
        page.mouse.click(start[0], start[1])
        out["start_button"] = start[2]
    else:
        page.mouse.click(W // 2, H // 2)
    page.wait_for_timeout(1500)
    # recorded, not scored (keeps scores comparable): does a person's click actually start the game? Without it
    # the checks below still run via the direct call, which can hide a start screen that swallows clicks.
    out["click_starts"] = bool(ev("() => !!document.pointerLockElement"))
    if not out["click_starts"]:
        ev("() => { const c = document.querySelector('canvas'); c && c.requestPointerLock && c.requestPointerLock(); }")
        page.wait_for_timeout(300)
    base = s.grab()
    if shot:
        shot.write_bytes(page.screenshot())
    q = base.quantize(colors=256)
    counts = sorted((c for c, _ in q.getcolors(256)), reverse=True)
    distinct = sum(1 for c in counts if c > 8)
    ok("renders_scene", gl and distinct >= 24 and counts[0] / sum(counts) < 0.85,
       f"{distinct} colours, dominant {counts[0] / sum(counts):.0%}")
    ok("no_page_errors", r is not None and r.ok and gl and not out["page_errors"],
       "; ".join(out["page_errors"][:3]) or ("" if gl else "needs a WebGL canvas"))
    ok("clean_console", gl and out["console_errors"] == 0, f"{out['console_errors']} console errors")
    overlay = ev("""() => [...document.body.querySelectorAll('*')].filter(e => {
        if (e.tagName === 'CANVAS' || e.tagName === 'SCRIPT' || e.tagName === 'STYLE') return false;
        const r = e.getBoundingClientRect(), st = getComputedStyle(e);
        return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && +st.opacity > 0.05
               && (e.innerText.trim() || st.backgroundColor !== 'rgba(0, 0, 0, 0)' || st.backgroundImage !== 'none'
                   || st.borderStyle !== 'none'); }).length""")
    ok("has_ui_overlay", gl and overlay > 0, f"{overlay} visible UI elements over the canvas")

    def idle(ms, box=None):
        a = s.grab()
        page.wait_for_timeout(ms)
        return s.diff(a, s.grab(), box)

    def responds(action, box=None, ms=600, factor=3.0, floor=0.01):
        """change caused by `action` vs the scene's own change over the same time"""
        noise = idle(ms, box)
        a = s.grab()
        action()
        page.wait_for_timeout(ms)
        d = s.diff(a, s.grab(), box)
        return d > max(floor, factor * noise), d, noise

    def api_count():
        return ev("() => (window.game && typeof window.game.blockCount === 'function') ? window.game.blockCount() : null")

    # break / place: change around the crosshair; re-aim lower (like a player) when nothing is in reach
    def look_down():
        ev("""() => { for (const t of [document, document.querySelector('canvas')]) if (t)
            t.dispatchEvent(new MouseEvent('mousemove', {movementX: 0, movementY: 60, bubbles: true})); }""")
        page.mouse.move(W // 2, H // 2 + 60, steps=3)
        page.wait_for_timeout(300)
        page.mouse.move(W // 2, H // 2)

    def click_test(button):
        best = (False, 0.0, 0.0)
        for attempt in range(4):
            got = responds(lambda: page.mouse.click(W // 2, H // 2, button=button), CENTER, ms=500)
            if got[0]:
                return got, attempt + 1
            best = max(best, got, key=lambda g: g[1])
            look_down()
        return best, 4
    n0 = api_count()
    (placed, d, noise), tries = click_test("right")
    ok("places_block", placed, f"crosshair change {d:.1%} vs idle {noise:.1%} (aim {tries})")
    (broke, d, noise), tries = click_test("left")
    ok("breaks_block", broke, f"crosshair change {d:.1%} vs idle {noise:.1%} (aim {tries})")

    def press_digits():
        for k in ("2", "3", "4"):
            page.keyboard.press(k)
            page.wait_for_timeout(120)
    got, d, noise = responds(press_digits, floor=0.001)      # a hotbar highlight is a small change
    ok("number_keys", got, f"change {d:.1%} vs idle {noise:.1%}")

    def look():
        page.mouse.move(W // 2 + 200, H // 2, steps=8)
        ev("""() => { for (const t of [document, document.querySelector('canvas')]) if (t)
            t.dispatchEvent(new MouseEvent('mousemove', {movementX: 120, movementY: 0, bubbles: true})); }""")
    got, d, noise = responds(look)
    ok("mouse_look", got, f"change {d:.1%} vs idle {noise:.1%}")
    page.mouse.move(W // 2, H // 2)

    best = (False, 0.0, 0.0)
    for k in ("w", "a", "s", "d"):
        def hold(k=k):
            page.keyboard.down(k)
            page.wait_for_timeout(900)
            page.keyboard.up(k)
        best = max(best, responds(hold, ms=300), key=lambda g: (g[0], g[1]))
    ok("walks", best[0], f"best of WASD: change {best[1]:.1%} vs idle {best[2]:.1%}")

    # jump: the view changes at the peak, then returns close to where it started
    noise = idle(400)
    before = s.grab()
    page.keyboard.down("Space")
    page.wait_for_timeout(120)
    page.keyboard.up("Space")
    page.wait_for_timeout(200)
    peak = s.diff(before, s.grab())
    page.wait_for_timeout(1800)
    after = s.diff(before, s.grab())
    ok("jumps", peak > max(0.01, 3 * noise) and after < peak, f"peak {peak:.1%}, settled {after:.1%}, idle {noise:.1%}")

    fps = ev("""() => new Promise(done => { let n = 0; const t0 = performance.now();
        function f() { n++; if (performance.now() - t0 < 3000) requestAnimationFrame(f);
                       else done(n * 1000 / (performance.now() - t0)); } requestAnimationFrame(f); })""")
    out["fps"] = round(fps, 1)
    ok("fps_15", gl and res["renders_scene"]["pass"] and fps >= 15, f"{fps:.1f} fps (software WebGL)")
    if n0 is not None:                                   # optional: a build that exposes an API
        out["api"] = {"block_count": n0}


def finish(out: dict) -> dict:
    passed = sum(c["pass"] for c in out["checks"].values())
    out["passed"], out["total"] = passed, len(CHECKS)
    out["score"] = round(100 * passed / len(CHECKS), 1)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--screenshot")
    a = ap.parse_args()
    t = time.time()
    r = score(Path(a.workdir), Path(a.screenshot) if a.screenshot else None)
    r["score_seconds"] = round(time.time() - t, 1)
    print(json.dumps(r))
