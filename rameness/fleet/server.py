"""Local HTTP server: JSON API + the single-page UI. The manager loop runs in a thread.

Binds to 127.0.0.1 by default; there is no authentication, so only expose it
(``--host 0.0.0.0``) on a network you trust or behind an authenticating proxy.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import metrics
from ..improve import Tuner
from .manager import Fleet

UI = Path(__file__).parent / "ui" / "index.html"


class Api:
    def __init__(self, fleet: Fleet):
        self.f = fleet
        self.tuner = Tuner(fleet.state)
        self.waiting: dict[str, queue.Queue] = {}      # "<turn>:<call id>" -> the client's tool result

    def turn(self, chat: str, body: dict, emit) -> None:
        """POST /api/chats/<chat>/turn, streamed as server-sent events. Tool calls go to the client, which posts
        each result to /api/turns/<turn>/results while the stream waits."""
        tid = "t-" + uuid.uuid4().hex[:8]

        def call_tool(c):
            key = f"{tid}:{c.id}"
            self.waiting[key] = q = queue.Queue(maxsize=1)
            try:
                emit({"t": "tool_call", "turn": tid, "id": c.id, "name": c.name, "input": c.input})
                return q.get(timeout=float(body.get("tool_timeout") or 120))
            except queue.Empty:
                return "ERROR: the client did not return a result"
            finally:
                self.waiting.pop(key, None)
        t0, first = time.time(), []

        def timed(ev):
            if ev.get("t") == "text" and ev.get("text", "").strip() and not first:
                first.append(time.time() - t0)
            emit(ev)
        emit({"t": "turn", "turn": tid})
        r = self.f.turn(body["text"], timed, chat=chat, mode=body.get("mode") or None,
                        tools=body.get("tools") or None, call_tool=call_tool, instructions=body.get("instructions") or "")
        metrics.inc("rameness_turns_total", route=r.get("route"))
        metrics.observe("rameness_turn_seconds", time.time() - t0, route=r.get("route"))
        if first:
            metrics.observe("rameness_turn_first_text_seconds", first[0], route=r.get("route"))

    def metrics_text(self) -> str:
        """GET /metrics: this process's counters plus the team's state, read from the store now."""
        db = self.f.store.db
        agents = [({"status": st, "role": role}, n) for st, role, n in
                  db.execute("SELECT status, role, COUNT(*) FROM agents GROUP BY status, role")]
        finished = [({"runtime": rt or "-", "outcome": st}, n) for rt, st, n in
                    db.execute("SELECT runtime, status, COUNT(*) FROM agents WHERE status IN ('done','failed') "
                               "AND role != 'manager' GROUP BY runtime, status")]
        decided = [({"gate": g or "-"}, n) for g, n in db.execute("SELECT gate, COUNT(*) FROM decisions GROUP BY gate")]
        return metrics.render([
            ("rameness_up", "1 while the manager is running.", [({"general": str(self.f.cfg["general"]).lower(),
                                                                   "work_mode": self.f.cfg["work_mode"]}, 1)]),
            ("rameness_agents", "Agents by status and role.", agents),
            ("rameness_agents_finished", "Finished agents by runtime and outcome.", finished),
            ("rameness_escalations_open", "Decisions waiting for the director.",
             [({}, db.execute("SELECT COUNT(*) FROM escalations WHERE status='open'").fetchone()[0])]),
            ("rameness_chats", "Chats the manager has.", [({}, db.execute("SELECT COUNT(*) FROM chats").fetchone()[0])]),
            ("rameness_decisions", "Decisions recorded, by who made them (jev, director).", decided),
        ])

    def chat_events(self, chat: str, since: int) -> dict:
        evs = self.f.store.events(since=since, chat=chat, limit=500)
        for e in evs:
            if e["type"] == "status" and e["message"].split(":")[0].endswith(("-> done", "-> failed")) and e["agent"]:
                a = self.f.store.agent(e["agent"]) or {}
                e["result"] = (a.get("result") or "")[-6000:]
                e["title"] = a.get("title")
        return {"events": evs, "last": evs[-1]["id"] if evs else since, "now": time.time()}

    def route(self, method: str, path: str, body: dict, query: dict):
        f = self.f
        if method == "GET" and path == "/api/state":
            return f.snapshot()
        if method == "GET" and path == "/api/shadow":
            since = float((query.get("since") or ["0"])[0])
            return {"decisions": f.store.decisions_since(since), "now": __import__("time").time()}
        if method == "GET" and path == "/api/chats":
            return {"chats": f.store.chats()}
        if method == "GET" and (m := re.fullmatch(r"/api/chats/([\w.:-]+)/events", path)):
            return self.chat_events(m.group(1), int((query.get("since") or ["0"])[0]))
        if method == "POST" and (m := re.fullmatch(r"/api/turns/([\w-]+)/results", path)):
            q = self.waiting.get(f"{m.group(1)}:{body['id']}")
            if q is None:
                raise LookupError("no tool call waiting with that id")
            q.put_nowait(str(body.get("content", "")))
            return {"ok": True}
        if method == "POST" and path == "/api/condense":
            return {"text": f.condense(body["text"], body.get("length") or "concise")}
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
                             categories=body.get("categories") or None, on=body.get("on") or None,
                             chat=body.get("chat") or None, mode=body.get("mode") or None)
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

        def _stream(self, fn):
            """Server-sent events: fn(emit) writes events until it returns."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            lock = threading.Lock()

            def emit(ev):
                with lock:
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()
            try:
                fn(emit)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                emit({"t": "error", "error": f"{type(e).__name__}: {e}"})

        def _handle(self, method):
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/index.html"):
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            body = {}
            if method in ("POST", "PUT"):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if method == "POST" and (m := re.fullmatch(r"/api/chats/([\w.:-]+)/turn", u.path)):
                return self._stream(lambda emit: api.turn(m.group(1), body, emit))
            if method == "GET" and u.path == "/metrics":
                return self._send(200, api.metrics_text().encode(), "text/plain; version=0.0.4; charset=utf-8")
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
