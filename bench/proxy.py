"""Metering proxy for benchmarks: sits between a harness and llama-server, forwards everything
unchanged (including streaming), and logs one JSON line per model request with tokens, tool calls
and latency - so every harness is measured the same way, whatever it reports itself.

    python -m bench.proxy --upstream http://10.0.0.187:8034 --port 18080 --log run.jsonl
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Meter:
    def __init__(self, log_path: str):
        self.lock = threading.Lock()
        self.f = open(log_path, "a", buffering=1)

    def write(self, rec: dict):
        with self.lock:
            self.f.write(json.dumps(rec) + "\n")


def usage_of(obj: dict, acc: dict) -> None:
    u = obj.get("usage") or {}
    if u.get("prompt_tokens") is not None or u.get("input_tokens") is not None:
        acc["prompt"] = u.get("prompt_tokens", u.get("input_tokens"))
        acc["completion"] = u.get("completion_tokens", u.get("output_tokens"))
    t = obj.get("timings") or {}
    if t and "prompt" not in acc:
        acc["prompt"] = (t.get("prompt_n") or 0) + (t.get("cache_n") or 0)
        acc["completion"] = t.get("predicted_n")
    for ch in obj.get("choices") or []:
        msg = ch.get("message") or ch.get("delta") or {}
        for tc in msg.get("tool_calls") or []:
            # streamed deltas repeat the index but carry the id only once: key by index when present
            acc.setdefault("tool_keys", set()).add(("i", tc["index"]) if tc.get("index") is not None else tc.get("id"))
        if ch.get("finish_reason"):
            acc["finish"] = ch["finish_reason"]
    # OpenAI Responses API events
    if obj.get("type") == "response.completed":
        r = obj.get("response") or {}
        usage_of({"usage": r.get("usage")}, acc)
        for item in r.get("output") or []:
            if item.get("type") == "function_call":
                acc.setdefault("tool_keys", set()).add(item.get("call_id"))


def make_handler(upstream: str, meter: Meter):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _forward(self, method: str):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            t0 = time.time()
            req = urllib.request.Request(upstream + self.path, data=body, method=method,
                                         headers={k: v for k, v in self.headers.items()
                                                  if k.lower() not in ("host", "content-length", "accept-encoding")})
            acc: dict = {}
            status = 502
            try:
                resp = urllib.request.urlopen(req, timeout=3600)
            except urllib.error.HTTPError as e:
                resp = e
            status = resp.status if hasattr(resp, "status") else resp.code
            ctype = resp.headers.get("Content-Type", "")
            self.send_response(status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                    self.send_header(k, v)
            if "event-stream" in ctype:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                buf = b""
                while True:
                    chunk = resp.readline()
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    line = chunk.strip()
                    if line.startswith(b"data:") and line[5:].strip() not in (b"[DONE]", b""):
                        try:
                            usage_of(json.loads(line[5:]), acc)
                        except Exception:
                            pass
                self.wfile.write(b"0\r\n\r\n")
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                try:
                    usage_of(json.loads(data), acc)
                except Exception:
                    pass
            if self.path.rstrip("/").endswith(("chat/completions", "responses", "completions")):
                req_json = {}
                try:
                    req_json = json.loads(body or b"{}")
                except Exception:
                    pass
                meter.write({"t": t0, "secs": round(time.time() - t0, 2), "path": self.path, "status": status,
                             "stream": bool(req_json.get("stream")), "n_tools_offered": len(req_json.get("tools") or []),
                             "prompt_tokens": acc.get("prompt"), "completion_tokens": acc.get("completion"),
                             "tool_calls": len(acc.get("tool_keys", ())), "finish": acc.get("finish")})

        def do_POST(self):
            self._forward("POST")

        def do_GET(self):
            self._forward("GET")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.upstream.rstrip("/"), Meter(a.log))).serve_forever()


if __name__ == "__main__":
    main()
