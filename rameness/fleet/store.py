"""Fleet state in SQLite (WAL): the org graph, events, decisions, escalations, messages.

State lives on disk so the manager, the watcher, the UI server and every worker
process can crash and restart without losing the fleet.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, parent TEXT, role TEXT, kind TEXT, title TEXT, task TEXT,
  status TEXT, runtime TEXT, slot TEXT, env TEXT, backend TEXT, handle TEXT,
  worktree TEXT, branch TEXT, result TEXT, depends TEXT DEFAULT '[]', forked_from TEXT,
  attempts INTEGER DEFAULT 0, meta TEXT DEFAULT '{}', created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, agent TEXT, type TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, t REAL, agent TEXT, question TEXT, probs TEXT,
  chosen TEXT, backend TEXT, outcome TEXT, query TEXT, gate TEXT, significance REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS escalations (id TEXT PRIMARY KEY, t REAL, agent TEXT, question TEXT, options TEXT,
  answer TEXT, status TEXT, kind TEXT, dkey TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, agent TEXT, sender TEXT,
  text TEXT, delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, uses INTEGER, ok INTEGER);
CREATE INDEX IF NOT EXISTS ev_agent ON events(agent);
"""

ACTIVE = ("queued", "starting", "running", "blocked", "paused")
JSON_COLS = ("depends", "meta", "handle")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.db as c:
            c.executescript(SCHEMA)
            for table, col, typ in (("decisions", "query", "TEXT"), ("decisions", "gate", "TEXT"),
                                    ("decisions", "significance", "REAL"), ("decisions", "reason", "TEXT"),
                                    ("escalations", "dkey", "TEXT"), ("escalations", "payload", "TEXT")):
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    # ---- agents (CRUD)

    def _row(self, r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS:
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k != "depends" else [])
        return d

    def create_agent(self, **f) -> dict:
        now = time.time()
        aid = f.pop("id", None) or f"{f.get('role', 'a')[0]}-{uuid.uuid4().hex[:6]}"
        f.setdefault("status", "queued")
        for k in JSON_COLS:
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k])
        cols = ["id", "created", "updated", *f]
        self.db.execute(f"INSERT INTO agents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [aid, now, now, *f.values()])
        self.event(aid, "created", f"{f.get('role')}: {f.get('title', '')}")
        return self.agent(aid)

    def agent(self, aid: str) -> dict | None:
        return self._row(self.db.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone())

    def agents(self, status: tuple | None = None, parent: str | None = "*") -> list[dict]:
        q, args = "SELECT * FROM agents WHERE 1=1", []
        if status:
            q += f" AND status IN ({','.join('?' * len(status))})"
            args += list(status)
        if parent != "*":
            q += " AND parent IS ?" if parent is None else " AND parent=?"
            args.append(parent)
        return [self._row(r) for r in self.db.execute(q + " ORDER BY created", args)]

    def update_agent(self, aid: str, **f) -> dict:
        for k in JSON_COLS:
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k])
        f["updated"] = time.time()
        self.db.execute(f"UPDATE agents SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", [*f.values(), aid])
        return self.agent(aid)

    def set_status(self, aid: str, status: str, message: str = "") -> None:
        old = self.agent(aid)
        if old and old["status"] != status:
            self.update_agent(aid, status=status)
            self.event(aid, "status", f"{old['status']} -> {status}" + (f": {message}" if message else ""))

    def delete_agent(self, aid: str) -> None:
        self.db.execute("DELETE FROM agents WHERE id=?", (aid,))
        self.event(aid, "deleted", "")

    def children(self, aid: str) -> list[dict]:
        return self.agents(parent=aid)

    def subtree(self, aid: str) -> list[dict]:
        out = []
        for c in self.children(aid):
            out.append(c)
            out += self.subtree(c["id"])
        return out

    # ---- events / decisions / escalations / messages

    def event(self, agent: str | None, type_: str, message: str) -> None:
        self.db.execute("INSERT INTO events (t, agent, type, message) VALUES (?,?,?,?)",
                        (time.time(), agent, type_, message[:2000]))

    def events(self, since: int = 0, agent: str | None = None, limit: int = 200) -> list[dict]:
        q, a = "SELECT * FROM events WHERE id>?", [since]
        if agent:
            q += " AND agent=?"
            a.append(agent)
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*a, limit]).fetchall()
        return [dict(r) for r in reversed(rows)]

    def decision(self, did: str, agent: str | None, question: str, probs: dict, chosen: str, backend: str,
                 query: str = "", gate: str = "jev", significance: float | None = None, reason: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO decisions (id,t,agent,question,probs,chosen,backend,query,gate,"
                        "significance,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (did, time.time(), agent, question, json.dumps(probs), chosen, backend, query[:600], gate,
                         significance, reason))

    def set_gate(self, did: str, gate: str, chosen: str | None = None) -> None:
        if chosen:
            self.db.execute("UPDATE decisions SET gate=?, chosen=? WHERE id=?", (gate, chosen, did))
        else:
            self.db.execute("UPDATE decisions SET gate=? WHERE id=?", (gate, did))

    def decisions_since(self, t: float, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT * FROM decisions WHERE t>? ORDER BY t LIMIT ?", (t, limit)).fetchall()
        return [{**dict(r), "probs": json.loads(r["probs"])} for r in rows]

    def decision_outcome(self, did: str, outcome: str) -> None:
        self.db.execute("UPDATE decisions SET outcome=? WHERE id=?", (outcome, did))

    def decisions(self, agent: str | None = None, limit: int = 100) -> list[dict]:
        q, a = "SELECT * FROM decisions", []
        if agent:
            q += " WHERE agent=?"
            a.append(agent)
        rows = self.db.execute(q + " ORDER BY t DESC LIMIT ?", [*a, limit]).fetchall()
        return [{**dict(r), "probs": json.loads(r["probs"])} for r in rows]

    def escalate(self, agent: str | None, question: str, options: list[str] | None = None, kind: str = "question",
                 dkey: str | None = None, payload: dict | None = None) -> str:
        eid = "e-" + uuid.uuid4().hex[:6]
        self.db.execute("INSERT INTO escalations (id,t,agent,question,options,status,kind,dkey,payload) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (eid, time.time(), agent, question, json.dumps(options or []), "open", kind, dkey,
                         json.dumps(payload or {})))
        self.event(agent, "escalation", question)
        return eid

    @staticmethod
    def _esc(r) -> dict:
        return {**dict(r), "options": json.loads(r["options"]), "payload": json.loads(r["payload"] or "{}")}

    def escalations(self, status: str | None = "open") -> list[dict]:
        q = "SELECT * FROM escalations" + (" WHERE status=?" if status else "") + " ORDER BY t"
        return [self._esc(r) for r in self.db.execute(q, (status,) if status else ()).fetchall()]

    def escalation(self, eid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM escalations WHERE id=?", (eid,)).fetchone()
        return self._esc(r) if r else None

    def escalation_by_key(self, dkey: str) -> dict | None:
        r = self.db.execute("SELECT * FROM escalations WHERE dkey=? ORDER BY t DESC LIMIT 1", (dkey,)).fetchone()
        return self._esc(r) if r else None

    def answer(self, eid: str, answer: str) -> None:
        self.db.execute("UPDATE escalations SET answer=?, status='answered' WHERE id=?", (answer, eid))
        e = self.escalation(eid)
        self.event(e["agent"] if e else None, "answered", f"{eid}: {answer}")

    def send(self, agent: str, text: str, sender: str = "director") -> None:
        self.db.execute("INSERT INTO messages (t, agent, sender, text) VALUES (?,?,?,?)", (time.time(), agent, sender, text))
        self.event(agent, "message", f"{sender}: {text[:200]}")

    def inbox(self, agent: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM messages WHERE agent=? AND delivered=0 ORDER BY id", (agent,)).fetchall()
        if rows:
            self.db.execute(f"UPDATE messages SET delivered=1 WHERE id IN ({','.join(str(r['id']) for r in rows)})")
        return [dict(r) for r in rows]

    # ---- outcome stats (priors for the JEV)

    def bump(self, key: str, ok: bool) -> None:
        self.db.execute("INSERT INTO stats (key, uses, ok) VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET "
                        "uses=uses+1, ok=ok+?", (key, int(ok), int(ok)))

    def rate(self, key: str) -> float:
        r = self.db.execute("SELECT uses, ok FROM stats WHERE key=?", (key,)).fetchone()
        return ((r["ok"] if r else 0) + 1) / ((r["uses"] if r else 0) + 2)
