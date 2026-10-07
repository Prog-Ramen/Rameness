"""Ship benchmark run logs to Loki, one readable event per line.

Every harness keeps its own log format. This turns each into the same event shape and pushes it to
Loki with low-cardinality labels, so Grafana can show any run's timeline and compare harnesses:

    labels     job="bench", batch, run, harness, model, source
    source     agent   what the agent did: its text, thinking, tool calls and results, compactions
               llm     one line per model request (from the recording proxy)
               jev     rameness only: every JEV decision (question, answer, probabilities)
               runner  run start / end / score
    metadata   kind (text | thinking | tool_call | tool_result | compaction | request | decision | ...),
               tool, ok, turn   (structured metadata: filter with  | kind="tool_call")

Sources per harness:
    claude-code  stdout.log (stream-json)          pi   stdout.log (--mode json)
    dsh          dsh-home/sessions/**/session.v3.jsonl.zstd (timestamped, incl. compaction attempts)
    rameness     events.jsonl (RAMENESS_EVENT_LOG) and work/.rameness/decisions.jsonl

Live: run.py starts a Shipper per run.  Finished runs:
    python -m bench.loki /root/bench/results/minecraft/<batch> [...]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

LOKI_URL = os.environ.get("LOKI_URL", "http://127.0.0.1:3100")
MAX_TEXT = 6000


def _clip(s, n=MAX_TEXT) -> str:
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s if len(s) <= n else s[:n] + f" …[+{len(s) - n} chars]"


def _ev(kind, text, t=None, **meta) -> dict:
    return {"t": t, "kind": kind, "text": _clip(text), **{k: v for k, v in meta.items() if v is not None}}


def _tool_summary(name: str, args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return f"{name} {_clip(args, 1500)}"
    if not isinstance(args, dict):
        return f"{name} {_clip(args, 1500)}"
    a = dict(args)
    for k in ("content", "new_string", "new", "newText", "new_str"):     # file bodies: show size, not text
        if isinstance(a.get(k), str) and len(a[k]) > 300:
            a[k] = f"<{len(a[k])} chars, {a[k].count(chr(10)) + 1} lines>"
    return f"{name} {_clip(json.dumps(a), 2500)}"


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            out.append(b.get("text") or (_text_of(b["content"]) if "content" in b else ""))
    return "\n".join(x for x in out if x)


# --------------------------------------------------------------------------- per-harness parsers
# each takes one decoded log record and returns a list of events (t=None when the source has no clock)

def parse_claude(e: dict) -> list[dict]:
    t, st = e.get("type"), e.get("subtype")
    if t == "assistant":
        out = []
        for b in e["message"].get("content", []):
            if b.get("type") == "thinking" and b.get("thinking"):
                out.append(_ev("thinking", b["thinking"]))
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append(_ev("text", b["text"]))
            elif b.get("type") == "tool_use":
                out.append(_ev("tool_call", _tool_summary(b["name"], b.get("input")), tool=b["name"]))
        return out
    if t == "user":
        c = e["message"].get("content")
        return [_ev("tool_result", _text_of(b.get("content")), ok=not b.get("is_error"))
                for b in (c if isinstance(c, list) else []) if isinstance(b, dict) and b.get("type") == "tool_result"]
    if t == "system" and st and "compact" in st:
        return [_ev("compaction", json.dumps(e.get("compact_metadata") or {k: v for k, v in e.items() if k != "type"}))]
    if t == "result":
        return [_ev("end", f"{st} turns={e.get('num_turns')} cost=${e.get('total_cost_usd')} "
                           f"{'ERROR ' if e.get('is_error') else ''}{e.get('result', '')}",
                    ok=not e.get("is_error"))]
    return []


def parse_pi(e: dict) -> list[dict]:
    t = e.get("type", "")
    if t == "turn_end":
        m = e.get("message") or {}
        ts = m.get("timestamp")
        ts = ts / 1000 if isinstance(ts, (int, float)) and ts > 1e11 else ts
        out = []
        for b in m.get("content", []):
            if b.get("type") == "thinking" and b.get("thinking"):
                out.append(_ev("thinking", b["thinking"], ts))
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append(_ev("text", b["text"], ts))
            elif b.get("type") in ("toolCall", "tool_call", "tool_use"):
                out.append(_ev("tool_call", _tool_summary(b.get("name"), b.get("arguments") or b.get("input")), ts,
                               tool=b.get("name")))
        return out
    if t == "tool_execution_end":
        return [_ev("tool_result", _text_of((e.get("result") or {}).get("content")), tool=e.get("toolName"),
                    ok=not e.get("isError"))]
    if "compact" in t:
        return [_ev("compaction", f"{t} {_clip({k: v for k, v in e.items() if k != 'type'}, 1500)}")]
    if t == "agent_end":
        return [_ev("end", "agent_end")]
    return []


def parse_dsh(e: dict) -> list[dict]:
    t, d = e.get("type", ""), e.get("data") or {}
    ts = e["time"] / 1000 if isinstance(e.get("time"), (int, float)) else None
    turn = d.get("step")
    if t == "assistant/message":
        out = []
        for b in (d.get("message") or {}).get("content", []):
            if b.get("type") == "reasoning" and b.get("text"):
                out.append(_ev("thinking", b["text"], ts, turn=turn))
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append(_ev("text", b["text"], ts, turn=turn))
        return out
    if t == "tool/call":
        return [_ev("tool_call", _tool_summary(d.get("name"), d.get("arguments")), ts, tool=d.get("name"), turn=turn)]
    if t == "tool/result":
        c = (d.get("message") or {}).get("content") or []
        err = any(isinstance(b, dict) and (b.get("isError") or b.get("is_error")) for b in c)
        return [_ev("tool_result", _text_of(c), ts, ok=not err, turn=turn)]
    if t.startswith("compaction/"):
        return [_ev("compaction", f"{t} {_clip(d, 1500)}", ts, ok=not d.get("error"))]
    if t == "user/message":
        return [_ev("prompt", _text_of(d.get("content")), ts)]
    return []


def parse_rameness(e: dict) -> list[dict]:
    k, t = e.get("kind"), e.get("t")
    if k == "tool":
        return [_ev("tool_call", _tool_summary(e["tool"], e.get("input")), t, tool=e["tool"], turn=e.get("turn")),
                _ev("tool_result", e.get("output", ""), t + e.get("seconds", 0), tool=e["tool"], ok=e.get("ok"),
                    turn=e.get("turn"))]
    if k == "llm":
        out = [_ev("thinking", e["reasoning"], t, turn=e.get("turn"))] if e.get("reasoning", "").strip() else []
        out += [_ev("text", e["text"], t, turn=e.get("turn"))] if e.get("text", "").strip() else []
        return out + [_ev("turn", f"turn {e.get('turn')}: {e.get('seconds')}s stop={e.get('stop_reason')} "
                                  f"tools={','.join(e.get('tools') or []) or '-'} usage={json.dumps(e.get('usage'))}",
                          t, turn=e.get("turn"))]
    if k in ("loop_guard", "progress_review"):
        v = e.get("action") or e.get("verdict")
        return [_ev(k, f"{k.replace('_', ' ')}: {v} probs={json.dumps(e.get('probs'))}"
                       + (f" signals={'; '.join(e.get('signals') or [])}" if e.get("signals") else "")
                       + ("" if e.get("acted", True) else " (no action)"), t, turn=e.get("turn"))]
    if k == "compaction":
        return [_ev("compaction", f"~{e.get('tokens_before')} -> ~{e.get('tokens_after')} tokens", t, turn=e.get("turn"))]
    return [_ev(k or "event", json.dumps({x: y for x, y in e.items() if x not in ("t", "kind")}, default=str), t)]


def parse_decision(e: dict) -> list[dict]:
    probs = e.get("probs") or {}
    best = max(probs, key=probs.get) if probs else "?"
    return [_ev("decision", f"{e.get('question')} -> {best} ({probs.get(best, 0):.2f}) "
                            f"probs={json.dumps(probs)} backend={e.get('backend')} | {_clip(e.get('query', ''), 600)}",
                e.get("t"))]


def parse_request(d: dict) -> list[dict]:
    return [_ev("request", f"status={d.get('status')} finish={d.get('finish_reason') or d.get('stop_reason')} "
                           f"prompt={d.get('prompt_tokens')} cached={d.get('cached_tokens')} "
                           f"out={d.get('completion_tokens')} ctx={d.get('context_tokens')} "
                           f"{d.get('request_seconds')}s ttft={d.get('ttft_seconds')} gen_tps={d.get('gen_tps')} "
                           f"tools={d.get('tool_calls')}" + (" COMPACTED" if d.get("compaction") else ""),
                d.get("t_start"), ok=d.get("status") == 200)]


# --------------------------------------------------------------------------- sources (incremental readers)

class JsonlTail:
    """Reads the complete new lines of a growing JSONL file and parses each record."""

    def __init__(self, path: Path, parse, source: str):
        self.path, self.parse, self.source, self.pos = path, parse, source, 0

    def poll(self) -> list[dict]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        self.pos += end
        out = []
        for ln in data[:end].splitlines():
            try:
                rec = json.loads(ln)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            try:
                out += [{**ev, "source": self.source} for ev in self.parse(rec)]
            except Exception:                              # one odd record must not stop the shipper
                continue
        return out


class DshSession:
    """dsh's zstd session log: re-decoded on each poll, new records picked by their sequence number."""

    def __init__(self, home: Path):
        self.home, self.seq = home, -1

    def poll(self) -> list[dict]:
        out = []
        for f in sorted(self.home.glob("sessions/*/*/session.v3.jsonl.zstd")):
            p = subprocess.run(["zstd", "-dcq", str(f)], capture_output=True)
            for ln in p.stdout.splitlines():
                try:
                    e = json.loads(ln)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if e.get("seq", -1) <= self.seq:
                    continue
                self.seq = e.get("seq", self.seq)
                out += [{**ev, "source": "agent"} for ev in parse_dsh(e)]
        return out


def sources_for(harness: str, rec_dir: Path, run_dir: Path, work: Path) -> list:
    s = [JsonlTail(rec_dir / "llm.jsonl", parse_request, "llm")]
    if harness == "claude-code":
        s.append(JsonlTail(rec_dir / "stdout.log", parse_claude, "agent"))
    elif harness == "pi":
        s.append(JsonlTail(rec_dir / "stdout.log", parse_pi, "agent"))
    elif harness == "dsh":
        s.append(DshSession(run_dir / "dsh-home"))
    elif harness == "rameness":
        s.append(JsonlTail(run_dir / "events.jsonl", parse_rameness, "agent"))
        s.append(JsonlTail(work / ".rameness" / "decisions.jsonl", parse_decision, "jev"))
    return s


# --------------------------------------------------------------------------- push

def push(labels: dict, events: list[dict]) -> None:
    streams: dict[str, dict] = {}
    for i, ev in enumerate(events):
        lab = {"job": "bench", **labels, "source": ev["source"]}
        key = json.dumps(lab, sort_keys=True)
        ns = str(int(ev["t"] * 1e9) + i % 1000)            # keep same-instant events in order
        meta = {k: str(ev[k]) for k in ("kind", "tool", "ok", "turn") if k in ev}
        streams.setdefault(key, {"stream": lab, "values": []})["values"].append(
            [ns, f"{ev['kind']:<12} {ev['text']}", meta])
    body = list(streams.values())
    for st in body:                          # Loki refuses entries far behind the newest one already in a stream
        st["values"].sort(key=lambda v: int(v[0]))
    for i in range(0, len(body), 20):
        req = urllib.request.Request(f"{LOKI_URL}/loki/api/v1/push",
                                     json.dumps({"streams": body[i:i + 20]}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:                             # logging must never break a benchmark run
            detail = e.read()[:300].decode(errors="replace") if hasattr(e, "read") else ""
            print(f"[loki] push failed: {e} {detail}", file=sys.stderr, flush=True)


class Shipper(threading.Thread):
    """Polls a run's log sources every few seconds and pushes new events, stamped with their own
    time or, for sources without a clock, the time they were seen."""

    def __init__(self, labels: dict, sources: list, every: float = 5.0):
        super().__init__(daemon=True)
        self.labels, self.sources, self.every = labels, sources, every
        self.stop_ev = threading.Event()

    def flush(self) -> None:
        now = time.time()
        events = []
        for s in self.sources:
            events += s.poll()
        for i, ev in enumerate(events):
            if ev.get("t") is None:
                ev["t"] = now + i * 1e-6
        if events:
            push(self.labels, events)

    def log(self, kind: str, text: str, **meta) -> None:
        push(self.labels, [{**_ev(kind, text, time.time(), **meta), "source": "runner"}])

    def run(self) -> None:
        while not self.stop_ev.wait(self.every):
            self.flush()

    def stop(self) -> None:
        self.stop_ev.set()
        self.join(timeout=30)
        self.flush()


# --------------------------------------------------------------------------- backfill finished runs

def backfill(batch: Path) -> None:
    for res in sorted(batch.glob("*/result.json")):
        d = res.parent
        r = json.loads(res.read_text())
        run_dir = Path("/home/bench/runs") / r["run"]
        if not run_dir.exists():
            run_dir = d
        work = d / "build" if (d / "build" / ".rameness").exists() else run_dir / "work"
        labels = {"batch": r["run"].rsplit(f"-{r['harness']}-", 1)[0], "run": r["run"],
                  "harness": r["harness"], "model": r["model"]}
        events = [ev for s in sources_for(r["harness"], d, run_dir, work) for ev in s.poll()]
        if r["harness"] == "rameness" and not (run_dir / "events.jsonl").exists():
            # runs from before the event log: its stderr ("  -> tool {...}" lines and [rameness] notes)
            for ln in (d / "stderr.log").read_text(errors="replace").splitlines():
                if ln.startswith("  -> "):
                    events.append({**_ev("tool_call", ln[5:], tool=ln[5:].split(" ", 1)[0]), "source": "agent"})
                elif ln.strip():
                    events.append({**_ev("note", ln.strip()), "source": "agent"})
        reqs = [json.loads(ln) for ln in (d / "llm.jsonl").read_text().splitlines()] if (d / "llm.jsonl").exists() else []
        _date([ev for ev in events if ev["source"] == "agent"], reqs)
        for ev in events:
            ev["t"] = ev["t"] or t_first(reqs)
        t_end = max((q.get("t_end") or 0 for q in reqs), default=time.time())
        events.append({**_ev("end", f"score={r.get('score')}% passed={r.get('passed')}/14 wall={r.get('wall_seconds')}s "
                                    f"timed_out={r.get('timed_out')} exit={r.get('exit_code')} "
                                    f"failed={[k for k, v in (r.get('checks') or {}).items() if not v]}", t_end),
                       "source": "runner"})
        push(labels, events)
        print(f"{r['run']}: {len(events)} events")


def t_first(reqs: list[dict]) -> float:
    return reqs[0]["t_start"] if reqs else time.time()


def _date(events: list[dict], reqs: list[dict]) -> None:
    """Give clockless events (Claude Code, older logs) the time of the model request that produced them:
    the n-th tool call belongs to the request whose cumulative tool-call count covers n."""
    if not reqs:
        for ev in events:
            ev["t"] = ev["t"] or time.time()
        return
    ends, n = [], 0
    for q in reqs:
        n += q.get("tool_calls") or 0
        ends.append((n, q.get("t_end") or q.get("t_start")))
    calls, last = 0, reqs[0].get("t_start")
    for i, ev in enumerate(events):
        if ev.get("t") is not None:
            last = ev["t"]
            continue
        if ev["kind"] == "tool_call":
            calls += 1
            last = next((t for c, t in ends if c >= calls), ends[-1][1])
        ev["t"] = last + i * 1e-6


def flush() -> None:
    """Ask the ingester to write its chunks to storage. Loki's query frontend sends only recent time
    ranges to the ingester, so backfilled (hours old) entries are invisible until flushed."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"{LOKI_URL}/flush", method="POST"), timeout=60).read()
    except Exception as e:
        print(f"[loki] flush failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    for b in sys.argv[1:]:
        backfill(Path(b))
    flush()
