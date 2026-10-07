"""A recording proxy between an agent harness and its model endpoint.

Every harness talks to its model through one of these, so speed, context and token telemetry is
measured the same way for all of them, whatever each harness logs itself. It forwards requests
unchanged (streaming included) and records, per model request:

* latency, time to first token, HTTP status
* prompt / cached / completion tokens and the context in use (prompt + completion)
* prompt-processing and generation speed (llama-server ``timings``; otherwise derived)
* tool calls requested, speculative-decoding acceptance (llama-server ``draft_n``)

It speaks both OpenAI Chat Completions (llama-server, pi, dsh, rameness) and Anthropic Messages
(Claude Code, against Anthropic or llama-server's /v1/messages). Each record is appended to a
JSONL file and pushed to VictoriaMetrics with the run's labels.
"""

from __future__ import annotations

import http.client
import json
import re
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import vm

HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "accept-encoding", "upgrade",
       "proxy-connection", "te", "trailer"}


def mark_compactions(records: list[dict], drop: float = 0.7, min_prompt: int = 8000) -> int:
    """Flag requests where the main conversation's prompt shrank sharply: the harness compacted.

    The main conversation is the lineage with the most tools (side requests such as title
    generation or summarisation carry fewer). A compaction is a main-lineage request whose prompt
    is below ``drop`` x the previous main request's, when that one was at least ``min_prompt``.
    """
    most, prev, n = 0, None, 0
    for r in records:
        r["compaction"] = 0
        if r.get("status") != 200 or not r.get("prompt_tokens"):
            continue
        t = r.get("n_tools") or 0
        if t > most:
            most, prev = t, None                      # a richer lineage appeared: that is the main thread
        if t != most or most == 0:
            continue
        if prev and prev >= min_prompt and r["prompt_tokens"] < drop * prev:
            r["compaction"] = 1
            n += 1
        prev = r["prompt_tokens"]
    return n


class Recorder:
    def __init__(self, labels: dict, jsonl: Path | None):
        self.labels, self.jsonl = labels, jsonl
        self.lock = threading.Lock()
        self.records: list[dict] = []

    def add(self, rec: dict) -> None:
        with self.lock:
            self.records.append(rec)
            mark_compactions(self.records)
            tot = self.totals()
        if self.jsonl:
            with self.lock, self.jsonl.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        lab = self.labels
        s = [(f"bench_llm_{k}", rec.get(k), lab) for k in (
            "request_seconds", "ttft_seconds", "prompt_tokens", "cached_tokens", "completion_tokens",
            "context_tokens", "prompt_tps", "gen_tps", "tool_calls", "draft_accept_ratio")]
        s.append(("bench_llm_http_status", rec["status"], lab))
        s.append(("bench_llm_compaction", rec.get("compaction", 0), lab))
        s += [(f"bench_run_{k}", v, lab) for k, v in tot.items()]
        vm.push(s, ts=rec["t_end"])

    def totals(self) -> dict:
        r = [x for x in self.records if x["status"] == 200]
        gen = [x["gen_tps"] for x in r if x.get("gen_tps")]
        return {
            "llm_requests": len(self.records),
            "llm_errors": sum(1 for x in self.records if x["status"] != 200),
            "prompt_tokens_total": sum(x.get("prompt_tokens") or 0 for x in r),
            "cached_tokens_total": sum(x.get("cached_tokens") or 0 for x in r),
            "completion_tokens_total": sum(x.get("completion_tokens") or 0 for x in r),
            "context_tokens_max": max((x.get("context_tokens") or 0 for x in r), default=0),
            "tool_calls_total": sum(x.get("tool_calls") or 0 for x in r),
            "compactions": sum(x.get("compaction") or 0 for x in r),
            "llm_seconds_total": round(sum(x["request_seconds"] for x in self.records), 3),
            "gen_tps_mean": round(sum(gen) / len(gen), 2) if gen else None,
        }


def _openai_usage(rec: dict, obj: dict) -> None:
    u = obj.get("usage") or {}
    t = obj.get("timings") or {}
    if u:
        rec["prompt_tokens"] = u.get("prompt_tokens")
        rec["completion_tokens"] = u.get("completion_tokens")
        rec["cached_tokens"] = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if t:
        rec["cached_tokens"] = t.get("cache_n", rec.get("cached_tokens"))
        rec["prompt_tps"] = t.get("prompt_per_second")
        rec["gen_tps"] = t.get("predicted_per_second")
        if rec.get("prompt_tokens") is None:
            rec["prompt_tokens"] = (t.get("prompt_n") or 0) + (t.get("cache_n") or 0)
        if rec.get("completion_tokens") is None:
            rec["completion_tokens"] = t.get("predicted_n")
        if t.get("draft_n"):
            rec["draft_accept_ratio"] = round(t.get("draft_n_accepted", 0) / t["draft_n"], 3)


def _anthropic_usage(rec: dict, u: dict) -> None:
    if not u:
        return
    if "input_tokens" in u:
        cached = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        rec["prompt_tokens"] = (u.get("input_tokens") or 0) + cached
        rec["cached_tokens"] = u.get("cache_read_input_tokens") or 0
    if u.get("output_tokens") is not None:
        rec["completion_tokens"] = u["output_tokens"]


NO_IMAGE = "[image omitted: this model cannot see images; inspect the output as text instead]"


def _strip_images(messages: list) -> int:
    """Replace image parts (Anthropic ``image`` blocks, also inside tool results; OpenAI ``image_url``
    parts) with a text note, in place. Returns how many were replaced."""
    n = 0

    def walk(blocks):
        nonlocal n
        for i, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            if b.get("type") in ("image", "image_url", "input_image"):
                blocks[i] = {"type": "text", "text": NO_IMAGE}
                n += 1
            elif isinstance(b.get("content"), list):
                walk(b["content"])

    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            walk(m["content"])
    return n


class Handler(BaseHTTPRequestHandler):
    upstream: urllib.parse.ParseResult
    recorder: Recorder
    join_system = False
    props = None                      # served at /props when the upstream has none (TabbyAPI)
    budget_field = "reasoning_budget_tokens"   # vLLM: thinking_token_budget
    # A harness that sends its own per-request budget (Rameness's per-turn levels) gets it up to this; the model
    # entry's reasoning_budget is only the default for harnesses that send none. Until 2026-10-07 the default was
    # also the cap, so Rameness's 8192 and 16384 levels reached the model as 5000.
    budget_ceiling = 16384
    tool_adapter = None               # "glm": parse GLM tool-call text for runtimes without a GLM parser
    max_tokens_cap = None             # cap on one reply's tokens, for runtimes without a thinking budget
    reasoning_budget = None           # llama-server upstreams: cap thinking tokens per reply
    timeout = 3600

    def log_message(self, *a):
        pass

    def _conn(self):
        u = self.upstream
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=self.timeout)

    def _forward(self, method: str) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        path = self.upstream.path.rstrip("/") + self.path if self.upstream.path not in ("", "/") else self.path
        kind = "openai" if self.path.split("?")[0].endswith("/chat/completions") else \
               "anthropic" if self.path.split("?")[0].endswith("/messages") else None
        req = None
        if kind and body:
            try:
                req = json.loads(body)
                changed = False
                if kind == "openai" and req.get("stream"):
                    req.setdefault("stream_options", {})["include_usage"] = True
                    changed = True
                if kind == "anthropic" and self.join_system and isinstance(req.get("system"), list):
                    # llama-server maps each system block to its own system message, which chat templates
                    # like Qwen's reject ("System message must be at the beginning"). Same text, one block.
                    req["system"] = "\n\n".join(b.get("text", "") for b in req["system"] if isinstance(b, dict))
                    changed = True
                if kind == "anthropic" and self.join_system and any(
                        m.get("role") == "system" for m in req.get("messages") or []):
                    # Claude Code also sends system-role messages mid-conversation; llama-server passes them
                    # through and the template rejects them. Turn each into a user message where it stands:
                    # moving the text into the system prompt would change the prompt's prefix on every new
                    # message and defeat llama-server's prefix cache.
                    for m in req["messages"]:
                        if m.get("role") == "system":
                            c = m.get("content")
                            text = c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c or [])
                            m["role"], m["content"] = "user", f"<system-reminder>\n{text}\n</system-reminder>"
                    changed = True
                if self.join_system and not self.vision and _strip_images(req.get("messages") or []):
                    # a server without vision (no mmproj) answers any image with a 500 and harnesses give up.
                    # Tell the model instead, as text, and let it carry on.
                    changed = True
                if self.max_tokens_cap and kind == "openai":
                    # a runtime without a thinking budget (vLLM's V2 runner): bound one reply instead, so a
                    # runaway think cannot spend 32k tokens
                    for key in ("max_tokens", "max_completion_tokens"):
                        if key in req and req[key] and req[key] > self.max_tokens_cap:
                            req[key] = self.max_tokens_cap
                            changed = True
                if self.tool_adapter == "glm" and kind == "openai":
                    # its chat template drops image parts, so the server finds no media marker for the image:
                    # put llama.cpp's marker in the text where each image goes
                    for m in req.get("messages") or []:
                        c = m.get("content")
                        if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "image_url" for x in c):
                            out = []
                            for x in c:
                                if isinstance(x, dict) and x.get("type") == "image_url":
                                    out.append({"type": "text", "text": "<__media__>"})
                                out.append(x)
                            m["content"] = out
                            changed = True
                if self.tool_adapter == "glm" and kind == "openai" and req.get("tools"):
                    # the runtime does not end the turn after a GLM tool call: stop there instead
                    stop = req.get("stop") or []
                    req["stop"] = ([stop] if isinstance(stop, str) else list(stop)) + ["</tool_call>"]
                    for m in req.get("messages") or []:
                        # its chat template cannot render structured tool calls (the runtime hands it the arguments
                        # as a string): give earlier calls back as the GLM text the model itself wrote
                        if m.get("role") == "assistant" and m.get("tool_calls"):
                            m["content"] = (m.get("content") or "") + "".join(_glm_call_text(tc) for tc in m.pop("tool_calls"))
                        elif m.get("role") == "assistant" and m.get("content") is None:
                            m["content"] = ""
                    changed = True
                if self.reasoning_budget is not None and self.join_system:
                    # llama-server's per-request thinking cap: once spent it closes the think block and the
                    # model has to answer (call a tool) inside the same reply
                    asked = req.get(self.budget_field)
                    if asked is None and self.budget_field != "reasoning_budget_tokens":
                        # Rameness sends its per-turn budget as llama-server's field: rename it for this server
                        asked = req.pop("reasoning_budget_tokens", None)
                    req[self.budget_field] = (min(int(asked), max(self.budget_ceiling, self.reasoning_budget))
                                              if asked else self.reasoning_budget)
                    # on /v1/messages llama-server takes the budget from Anthropic's thinking.budget_tokens when
                    # present (Claude Code sends ~32k, or an adaptive form), overriding the field above: state it
                    if kind == "anthropic":
                        th = req.get("thinking") if isinstance(req.get("thinking"), dict) else {}
                        if th.get("type") != "disabled":
                            req["thinking"] = {"type": "enabled", "budget_tokens": min(
                                int(th.get("budget_tokens") or self.reasoning_budget), self.reasoning_budget)}
                    if os.environ.get("BENCH_PROXY_DUMP"):
                        Path(os.environ["BENCH_PROXY_DUMP"]).write_text(json.dumps(
                            {k: v for k, v in req.items() if k not in ("messages", "system", "tools")}))
                    changed = True
                if changed:
                    body = json.dumps(req).encode()
            except json.JSONDecodeError:
                req = None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Host"] = self.upstream.netloc
        headers["Accept-Encoding"] = "identity"
        if body:
            headers["Content-Length"] = str(len(body))
        t0 = time.time()
        conn = self._conn()
        self.open_conns.add(conn)
        stop = threading.Event()
        threading.Thread(target=self._drop_on_hangup, args=(conn, stop), daemon=True).start()
        self.hung_up = False
        try:
            self._relay(conn, method, path, body, headers, req, kind, t0)
        except OSError:
            if not self.hung_up:                             # otherwise: nobody left to answer
                raise
        finally:
            stop.set()
            self.open_conns.discard(conn)
            conn.close()

    def _drop_on_hangup(self, conn, stop: threading.Event) -> None:
        """A client that gives up (killed harness, timeout) must cancel its request: llama-server only stops
        generating when its own connection closes, and a non-streamed reply sends nothing to notice the hangup
        with until it is done - hours for a long reply on a slow server."""
        import select
        import socket
        while not stop.wait(2):
            try:
                r, _, _ = select.select([self.connection], [], [], 0)
                if r and not self.connection.recv(1, socket.MSG_PEEK):
                    break                                    # EOF: the client closed its end
            except (OSError, ValueError):
                break
        else:
            return
        self.hung_up = True
        _close_upstream(conn)

    def _relay(self, conn, method, path, body, headers, req, kind, t0) -> None:
        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            self.send_error(502, f"upstream: {e}")
            if kind:
                self.recorder.add({"t_start": t0, "t_end": time.time(), "api": kind, "status": 502,
                                   "request_seconds": round(time.time() - t0, 3), "error": str(e)})
            return
        if self.tool_adapter == "glm" and kind == "openai" and resp.status == 200 \
                and "text/event-stream" not in (resp.getheader("Content-Type") or ""):
            resp = _Buffered(resp, _glm_tool_calls(resp.read()))
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        rec = {"t_start": t0, "api": kind, "status": resp.status, "stream": bool(req and req.get("stream")),
               "n_messages": len((req or {}).get("messages") or []), "n_tools": len((req or {}).get("tools") or []),
               "model": (req or {}).get("model"), "tool_calls": 0}
        streaming = "text/event-stream" in (resp.getheader("Content-Type") or "")
        buf, raw, first, tool_idx = b"", [], None, set()
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            if not kind:
                continue
            if not streaming:
                raw.append(chunk)
                continue
            buf += chunk
            while b"\n" in buf:
                ln, buf = buf.split(b"\n", 1)
                ln = ln.strip()
                if not ln.startswith(b"data:"):
                    continue
                data = ln[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if kind == "openai":
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        if first is None and (d.get("content") or d.get("reasoning_content") or d.get("tool_calls")):
                            first = time.time()
                        for tc in d.get("tool_calls") or []:
                            tool_idx.add((ch.get("index", 0), tc.get("index", 0)))
                        if ch.get("finish_reason"):
                            rec["finish_reason"] = ch["finish_reason"]
                    _openai_usage(rec, obj)
                else:
                    t = obj.get("type")
                    if t == "message_start":
                        _anthropic_usage(rec, (obj.get("message") or {}).get("usage") or {})
                    elif t == "content_block_start" and (obj.get("content_block") or {}).get("type") == "tool_use":
                        rec["tool_calls"] += 1
                    elif t == "content_block_delta" and first is None:
                        first = time.time()
                    elif t == "message_delta":
                        _anthropic_usage(rec, obj.get("usage") or {})
                        rec["finish_reason"] = (obj.get("delta") or {}).get("stop_reason")
                    if obj.get("timings"):
                        _openai_usage(rec, {"timings": obj["timings"]})
        conn.close()
        if not kind:
            return
        t1 = time.time()
        if raw:
            try:
                obj = json.loads(b"".join(raw))
                if kind == "openai":
                    _openai_usage(rec, obj)
                    for ch in obj.get("choices") or []:
                        rec["tool_calls"] += len((ch.get("message") or {}).get("tool_calls") or [])
                        rec["finish_reason"] = ch.get("finish_reason")
                else:
                    _anthropic_usage(rec, obj.get("usage") or {})
                    rec["tool_calls"] += sum(1 for b in obj.get("content") or [] if b.get("type") == "tool_use")
                    rec["finish_reason"] = obj.get("stop_reason")
                    if obj.get("timings"):
                        _openai_usage(rec, {"timings": obj["timings"]})
            except (json.JSONDecodeError, AttributeError):
                pass
        if kind == "openai" and streaming:
            rec["tool_calls"] = len(tool_idx)
        rec["t_end"] = t1
        rec["request_seconds"] = round(t1 - t0, 3)
        rec["ttft_seconds"] = round(first - t0, 3) if first else None
        if rec.get("gen_tps") is None and (rec.get("completion_tokens") or 0) >= 32 and first and t1 > first:
            rec["gen_tps"] = round(rec["completion_tokens"] / (t1 - first), 2)
        rec["context_tokens"] = (rec.get("prompt_tokens") or 0) + (rec.get("completion_tokens") or 0) or None
        self.recorder.add(rec)

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        if self.props is not None and self.path.split("?")[0] == "/props":
            # an upstream without llama.cpp's /props (TabbyAPI): answer with the same fields, so the harness
            # sees the real window and vision support as it does on llama-server
            body = json.dumps(self.props).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._forward("GET")


_GLM_CALL = re.compile(r"<tool_call>\s*([^<\s]+)\s*((?:<arg_key>.*?</arg_key>\s*<arg_value>.*?</arg_value>\s*)*)(?:</tool_call>|$)", re.S)
_GLM_ARG = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.S)


def _glm_tool_calls(body: bytes) -> bytes:
    """GLM tool calls left as text by a runtime without a GLM parser (<tool_call>name<arg_key>k</arg_key><arg_value>v
    </arg_value></tool_call>) -> OpenAI tool_calls. Values that parse as JSON are passed as JSON, others as strings."""
    try:
        d = json.loads(body)
        msg = d["choices"][0]["message"]
    except Exception:
        return body
    text = msg.get("content") or ""
    calls = list(_GLM_CALL.finditer(text))
    if not calls or msg.get("tool_calls"):
        return body
    out = []
    for i, m in enumerate(calls):
        args = {}
        for k, v in _GLM_ARG.findall(m.group(2)):
            try:
                args[k.strip()] = json.loads(v)
            except ValueError:
                args[k.strip()] = v
        out.append({"id": f"call_glm_{int(time.time() * 1000)}_{i}", "type": "function",
                    "function": {"name": m.group(1).strip(), "arguments": json.dumps(args)}})
    msg["content"] = text[:calls[0].start()].strip() or None
    msg["tool_calls"] = out
    d["choices"][0]["finish_reason"] = "tool_calls"
    return json.dumps(d).encode()


def _glm_call_text(tc: dict) -> str:
    fn = tc.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        args = {}
    parts = "".join(f"<arg_key>{k}</arg_key><arg_value>{v if isinstance(v, str) else json.dumps(v)}</arg_value>"
                    for k, v in (args.items() if isinstance(args, dict) else []))
    return f"<tool_call>{fn.get('name', '')}{parts}</tool_call>"


class _Buffered:
    """A fully read upstream response with a replaced body (and Content-Length to match)."""
    def __init__(self, resp, body: bytes):
        self.status, self._io = resp.status, __import__("io").BytesIO(body)
        self._headers = [(k, str(len(body)) if k.lower() == "content-length" else v) for k, v in resp.getheaders()]
    def getheaders(self): return self._headers
    def getheader(self, name, default=None):
        return next((v for k, v in self._headers if k.lower() == name.lower()), default)
    def read(self, n=-1): return self._io.read(n)
    def read1(self, n=-1): return self._io.read(n)


def _upstream_vision(upstream: str) -> bool:
    """llama-server started with a vision projector says so at /props (modalities.vision)."""
    try:
        with urllib.request.urlopen(upstream.rstrip("/") + "/props", timeout=5) as r:
            return bool((json.load(r).get("modalities") or {}).get("vision"))
    except Exception:
        return False


def _close_upstream(conn) -> None:
    import socket
    try:
        if conn.sock:
            conn.sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


class Proxy:
    """``with Proxy(upstream, labels, jsonl) as p: ... p.url ...``"""

    def __init__(self, upstream: str, labels: dict, jsonl: Path | None = None, port: int = 0,
                 reasoning_budget: int | None = None, vision: bool | None = None, props: dict | None = None,
                 budget_field: str = "reasoning_budget_tokens", tool_adapter: str | None = None,
                 max_tokens_cap: int | None = None):
        self.recorder = Recorder(labels, jsonl)
        u = urllib.parse.urlparse(upstream)
        handler = type("H", (Handler,), {"upstream": u, "recorder": self.recorder,
                                         "join_system": u.hostname != "api.anthropic.com",
                                         "reasoning_budget": reasoning_budget, "open_conns": set(),
                                         # servers without llama.cpp's /props (TabbyAPI, vLLM) state it
                                         "vision": _upstream_vision(upstream) if vision is None else vision,
                                         "props": props, "budget_field": budget_field,
                                         "tool_adapter": tool_adapter, "max_tokens_cap": max_tokens_cap})
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        for conn in list(self.handler.open_conns):          # cancel whatever is still generating upstream
            _close_upstream(conn)
        self.server.shutdown()
        self.server.server_close()
