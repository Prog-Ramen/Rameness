"""Shared harness for end-to-end tests: every test drives the real `rameness` CLI / server
as separate processes, in a throwaway project with its own RAMENESS_HOME."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_URL = os.environ.get("RAMENESS_E2E_URL")          # e.g. http://10.0.0.187:8034 (llama-server)


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-c", "user.email=e2e@test", "-c", "user.name=e2e", *args], cwd=cwd,
                          capture_output=True, text=True, check=check)


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rameness-e2e-"))
        self.home = self.tmp / "home"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        # RAMENESS_GH=none: nothing in a test can reach real GitHub
        self.env = {**os.environ, "RAMENESS_HOME": str(self.home), "RAMENESS_GH": "none", "PYTHONPATH": str(ROOT),
                    "GIT_TERMINAL_PROMPT": "0"}
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        subprocess.run(["tmux", "kill-session", "-t", "rameness"], capture_output=True)

    # ---- helpers

    def cli(self, *args, cwd=None, replay=None, timeout=180, check=True, input=None, env=None):
        e = {**self.env, **(env or {})}
        if replay is not None:
            f = self.tmp / f"replay-{time.time_ns()}.json"
            f.write_text(json.dumps(replay))
            e["RAMENESS_REPLAY"] = str(f)
        p = subprocess.run([sys.executable, "-m", "rameness", *args], cwd=cwd or self.proj, env=e, input=input,
                           capture_output=True, text=True, timeout=timeout)
        if check and p.returncode:
            self.fail(f"rameness {' '.join(args)} exited {p.returncode}\nstdout:{p.stdout[-2000:]}\nstderr:{p.stderr[-3000:]}")
        return p

    def config(self, cfg: dict | None = None, fleet: dict | None = None, where: Path | None = None):
        d = (where or self.proj) / ".rameness"
        d.mkdir(parents=True, exist_ok=True)
        if cfg is not None:
            (d / "config.json").write_text(json.dumps(cfg))
        if fleet is not None:
            (d / "fleet.json").write_text(json.dumps(fleet))

    def repo(self, path: Path | None = None, files: dict | None = None) -> Path:
        path = path or self.proj
        path.mkdir(parents=True, exist_ok=True)
        git(path, "init", "-q", "-b", "main")
        for name, text in (files or {"README.md": "# demo\n"}).items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(text)
        (path / ".gitignore").write_text(".rameness/\n__pycache__/\n")
        git(path, "add", "-A")
        git(path, "commit", "-qm", "init")
        return path

    def bare(self, name: str) -> Path:
        p = self.tmp / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(p)], check=True)
        return p

    # ---- fleet server

    def serve(self, cwd: Path | None = None) -> str:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        log = open(self.tmp / f"server-{port}.log", "w")
        p = subprocess.Popen([sys.executable, "-m", "rameness", "fleet", "up", "--port", str(port)],
                             cwd=cwd or self.proj, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(p)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/api/state", timeout=2)
                return base
            except Exception:
                if p.poll() is not None:
                    self.fail("server exited: " + (self.tmp / f"server-{port}.log").read_text()[-2000:])
                time.sleep(0.2)
        self.fail("server did not start")

    def api(self, base: str, path: str, body: dict | None = None, method: str | None = None, ok: bool = True):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if ok:
                self.fail(f"{path}: HTTP {e.code} {e.read()[:500]}")
            return {"_status": e.code, **json.loads(e.read() or b"{}")}

    def until(self, fn, timeout=60, interval=0.4, msg=""):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            last = fn()
            if last:
                return last
            time.sleep(interval)
        self.fail(f"timed out: {msg} (last={str(last)[:300]})")

    def tree_ids(self, node) -> list[dict]:
        out = [node]
        for c in node["children"]:
            out += self.tree_ids(c)
        return out


# a fake CLI agent: research tasks print options JSON, testers print a report, others edit app.txt
FAKE_AGENT = r'''
case "$0" in
  *"Propose 3-6"*) printf 'Looked around.\n```json\n{"options": [{"title": "Add input validation", "why": "bad input crashes", "impact": "high", "effort": "small", "risk": "low"}, {"title": "Rewrite in Rust", "why": "speed", "impact": "low", "effort": "large", "risk": "high"}]}\n```\n';;
  *"Test type:"*) printf 'Tested.\n```json\n{"summary": "edge cases covered", "tests_run": 12, "tests_failed": 1, "tests_written": ["test_empty", "test_unicode", "test_timeout"], "covered": ["boundaries", "error paths", "invalid input"], "not_covered": [], "findings": [{"title": "Crash on empty input", "severity": "high", "details": "empty string raises"}]}\n```\n';;
  *"sleep"*) sleep 60;;
  *) echo "change" >> app.txt; echo "done and verified";;
esac
'''
