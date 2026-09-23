"""Local HTTP server: JSON API + the single-page UI. The manager loop runs in a thread.

Binds to 127.0.0.1 by default; there is no authentication, so only expose it
(``--host 0.0.0.0``) on a network you trust or behind an authenticating proxy.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..improve import Tuner
from .manager import Fleet

UI = Path(__file__).parent / "ui" / "index.html"


class Api:
    def __init__(self, fleet: Fleet):
        self.f = fleet
        self.tuner = Tuner(fleet.state)

    def route(self, method: str, path: str, body: dict, query: dict):
        f = self.f
        if method == "GET" and path == "/api/state":
            return f.snapshot()
        if method == "GET" and path == "/api/shadow":
            since = float((query.get("since") or ["0"])[0])
            return {"decisions": f.store.decisions_since(since), "now": __import__("time").time()}
        if m := re.fullmatch(r"/api/agents/([\w-]+)", path):
            aid = m.group(1)
            if method == "GET":
                a = f.get(aid)
                h = a.get("handle") or {}
                return {**f._public(a), "log": f.backend_for(h).read(h, 200) if h else "",
                        "decisions": f.store.decisions(agent=aid, limit=40),
                        "events": f.store.events(agent=aid, limit=80)}
            if method == "DELETE":
                f.retire(aid, delete=True)
                return {"ok": True}
        if method == "POST":
            if path == "/api/ask":
                cycles = body.get("cycles") or 0
                return f.ask(body["text"], cycles=cycles if cycles == "godmode" else int(cycles),
                             categories=body.get("categories") or None, on=body.get("on") or None)
            if path == "/api/autonomy":
                return {"autonomy": f.set_autonomy(body["mode"])}
            if m := re.fullmatch(r"/api/programs/([\w-]+)/stop", path):
                f.programs.stop(m.group(1))
                return {"ok": True}
            if path == "/api/agents":
                return f._public(f.spawn(body["task"], parent=body.get("parent") or "manager",
                                         role=body.get("role", "associate"), kind=body.get("kind", "deliver"),
                                         title=body.get("title"), slot=body.get("slot") or None,
                                         env=body.get("env") or None))
            if m := re.fullmatch(r"/api/agents/([\w-]+)/(\w+)", path):
                aid, action = m.groups()
                if action == "prompt":
                    return {"result": f.prompt(aid, body["text"])}
                if action == "reassign":
                    return f._public(f.reassign(aid, body.get("slot") or None, body.get("env") or None))
                if action in ("pause", "resume"):
                    getattr(f, action)(aid)
                    return {"ok": True}
                if action == "retire":
                    f.retire(aid, drop_branch=bool(body.get("drop_branch")))
                    return {"ok": True}
                if action == "fork":
                    return {"forks": [x["id"] for x in f.fork(aid, body.get("instructions"), body.get("n"))]}
            if m := re.fullmatch(r"/api/escalations/([\w-]+)", path):
                f.answer(m.group(1), body["answer"])
                return {"ok": True}
            if path == "/api/slots/scan":
                f.refresh_slots()
                return {"slots": len(f.slots)}
            if path == "/api/envs/probe":
                f.probe_envs()
                return {"envs": len(f.envs)}
            if path == "/api/config":
                for k in ("mode", "autonomous", "privacy", "max_active"):
                    if k in body:
                        f.cfg[k] = body[k]
                return f.snapshot()["config"]
            if path == "/api/jev/feedback":
                self.tuner.feedback(body["decision"], label=body.get("label"), correct=body.get("correct"))
                f.store.decision_outcome(body["decision"], "label:" + (body.get("label") or
                                                                        ("correct" if body.get("correct") else "")))
                return {"ok": True}
            if path == "/api/jev/calibrate":
                return {"weights": self.tuner.calibrate()}
            if path == "/api/jev/propose":
                if not f.planner:
                    raise ValueError("no model available to propose improvements")
                return {"proposals": self.tuner.propose(f.planner, body.get("question"))}
            if m := re.fullmatch(r"/api/jev/proposals/([\w-]+)", path):
                return self.tuner.resolve(m.group(1), bool(body.get("apply")))
            if path == "/api/jev/export":
                out = f.state / "jev_training.jsonl"
                return {"path": str(out), "rows": self.tuner.export(out)}
        if method == "GET" and path == "/api/jev":
            return {"stats": self.tuner.stats(), "tuning": self.tuner.tuning(), "proposals": self.tuner.proposals(),
                    "backend": f.jev.backend.name}
        raise LookupError(f"{method} {path}")


def make_handler(api: Api):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/index.html"):
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            body = {}
            if method in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                self._send(200, api.route(method, u.path, body, parse_qs(u.query)))
            except LookupError as e:
                self._send(404, {"error": str(e)})
            except (KeyError, ValueError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return H


def serve(fleet: Fleet, host: str = "127.0.0.1", port: int = 7788, loop: bool = True) -> None:
    stop = threading.Event()
    if loop:
        threading.Thread(target=fleet.run_forever, kwargs={"stop": stop}, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(Api(fleet)))
    fleet.out(f"rameness fleet: http://{host}:{port}  (backend={fleet.backend.name}, "
              f"planner={getattr(fleet.planner, 'slot_id', None)}, slots={len(fleet.slots)}, envs={len(fleet.envs)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
