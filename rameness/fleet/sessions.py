"""Session backends: where each agent's process lives so you can watch or attach to it.

* ``subprocess`` - headless, always available; attach = tail the log
* ``tmux``       - one window per agent in the ``rameness`` tmux session
* ``screen``     - one detached screen session per agent
* ``herdr``      - one tab per agent via herdr's socket API (agent-aware status)

Every backend runs the same wrapper, so status and output are uniform::

    <command> 2>&1 | tee -a <log>; echo ${PIPESTATUS[0]} > <exitfile>

Status: ``exited`` (exit file present, with code), ``running`` (backend says
alive), or ``lost`` (backend has no record and no exit file).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import uuid
from pathlib import Path


def wrapper(argv: list[str], log: Path, exitf: Path) -> str:
    cmd = " ".join(shlex.quote(a) for a in argv)
    return f"{cmd} 2>&1 | tee -a {shlex.quote(str(log))}; echo ${{PIPESTATUS[0]}} > {shlex.quote(str(exitf))}"


class Backend:
    name = "base"

    def __init__(self, state: Path):
        self.state = state / "sessions"
        self.state.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def available() -> bool:
        return True

    def _files(self, name: str) -> tuple[Path, Path]:
        return self.state / f"{name}.log", self.state / f"{name}.exit"

    def start(self, name: str, argv: list[str], cwd: str, env: dict | None = None) -> dict:
        log, exitf = self._files(name)
        exitf.unlink(missing_ok=True)
        h = {"backend": self.name, "name": name, "log": str(log), "exit": str(exitf)}
        h.update(self._start(name, wrapper(argv, log, exitf), cwd, env or {}))
        return h

    def _start(self, name, script, cwd, env) -> dict:
        raise NotImplementedError

    def alive(self, h: dict) -> bool:
        raise NotImplementedError

    def status(self, h: dict) -> tuple[str, int | None]:
        ex = Path(h["exit"])
        if ex.exists():
            try:
                return "exited", int(ex.read_text().strip() or 1)
            except ValueError:
                return "exited", 1
        return ("running", None) if self.alive(h) else ("lost", None)

    def read(self, h: dict, lines: int = 80) -> str:
        p = Path(h["log"])
        if not p.exists():
            return ""
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200 * lines))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])

    def send(self, h: dict, text: str) -> bool:
        return False

    def stop(self, h: dict) -> None:
        raise NotImplementedError

    def attach_cmd(self, h: dict) -> str:
        return f"tail -f {shlex.quote(h['log'])}"


class SubprocessBackend(Backend):
    name = "subprocess"

    def _start(self, name, script, cwd, env):
        p = subprocess.Popen(["bash", "-c", script], cwd=cwd, env={**os.environ, **env}, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"pid": p.pid}

    def alive(self, h):
        pid = h.get("pid")
        if not pid:
            return False
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:      # reap if it's our child
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop(self, h):
        try:
            os.killpg(h["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError, KeyError):
            pass


class TmuxBackend(Backend):
    name = "tmux"
    SESSION = "rameness"

    @staticmethod
    def available():
        return shutil.which("tmux") is not None

    def _tmux(self, *args, check=False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)

    def _start(self, name, script, cwd, env):
        if self._tmux("has-session", "-t", self.SESSION).returncode:
            self._tmux("new-session", "-d", "-s", self.SESSION, "-n", "fleet", check=True)
        envargs = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
        self._tmux("new-window", "-d", "-t", f"{self.SESSION}:", "-n", name, "-c", cwd, *envargs,
                   "bash", "-c", script + "; echo '[session ended - press enter]'; read", check=True)
        return {"target": f"{self.SESSION}:{name}"}

    def alive(self, h):
        r = self._tmux("list-windows", "-t", self.SESSION, "-F", "#{window_name}")
        return r.returncode == 0 and h["name"] in r.stdout.split()

    def send(self, h, text):
        self._tmux("send-keys", "-t", h["target"], "-l", text)
        return self._tmux("send-keys", "-t", h["target"], "Enter").returncode == 0

    def stop(self, h):
        self._tmux("kill-window", "-t", h["target"])

    def attach_cmd(self, h):
        return f"tmux attach -t {self.SESSION} \\; select-window -t {h['name']}"


class ScreenBackend(Backend):
    name = "screen"

    @staticmethod
    def available():
        return shutil.which("screen") is not None

    def _start(self, name, script, cwd, env):
        sname = f"rameness-{name}"
        envp = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        subprocess.run(["screen", "-dmS", sname, "bash", "-c", f"{envp} bash -c {shlex.quote(script)}" if envp else script],
                       cwd=cwd, check=True)
        return {"screen": sname}

    def alive(self, h):
        r = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        return f".{h['screen']}\t" in r.stdout or f".{h['screen']} " in r.stdout

    def send(self, h, text):
        return subprocess.run(["screen", "-S", h["screen"], "-X", "stuff", text + "\n"]).returncode == 0

    def stop(self, h):
        subprocess.run(["screen", "-S", h["screen"], "-X", "quit"], capture_output=True)

    def attach_cmd(self, h):
        return f"screen -r {h['screen']}"


class HerdrBackend(Backend):
    """herdr socket API (newline-delimited JSON). Each agent gets its own tab in a
    ``rameness`` workspace; herdr's agent detection supplies working/blocked/done."""

    name = "herdr"

    @staticmethod
    def socket_path() -> str:
        if os.environ.get("HERDR_SOCKET_PATH"):
            return os.environ["HERDR_SOCKET_PATH"]
        base = Path.home() / ".config" / "herdr"
        s = os.environ.get("HERDR_SESSION")
        return str(base / "sessions" / s / "herdr.sock") if s else str(base / "herdr.sock")

    @classmethod
    def available(cls):
        return shutil.which("herdr") is not None and os.path.exists(cls.socket_path())

    def call(self, method: str, **params):
        req = {"id": "jv_" + uuid.uuid4().hex[:8], "method": method, "params": params}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.socket_path())
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode().strip().splitlines()[0])
        if "error" in resp:
            raise RuntimeError(f"herdr {method}: {resp['error'].get('message')}")
        return resp.get("result", {})

    @staticmethod
    def _find(obj, key):
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                r = HerdrBackend._find(v, key)
                if r is not None:
                    return r
        if isinstance(obj, list):
            for v in obj:
                r = HerdrBackend._find(v, key)
                if r is not None:
                    return r
        return None

    def _workspace(self, cwd: str) -> str:
        res = self.call("workspace.list")
        for w in self._find(res, "workspaces") or []:
            if w.get("label") == "rameness":
                return w.get("workspace_id") or w.get("id")
        return self._find(self.call("workspace.create", cwd=cwd, label="rameness"), "workspace_id")

    def _start(self, name, script, cwd, env):
        ws = self._workspace(cwd)
        res = self.call("layout.apply", workspace_id=ws, tab_label=name, focus=False,
                        root={"type": "pane", "label": name, "cwd": cwd, "command": ["bash", "-c", script],
                              "env": {**env, "HERDR_ROLE": "rameness-agent"}})
        return {"workspace_id": ws, "pane_id": self._find(res, "pane_id")}

    def alive(self, h):
        try:
            return self.call("pane.get", pane_id=h["pane_id"]) is not None
        except Exception:
            return False

    def agent_status(self, h) -> str | None:
        try:
            return self._find(self.call("pane.get", pane_id=h["pane_id"]), "agent_status")
        except Exception:
            return None

    def send(self, h, text):
        try:
            self.call("pane.send_text", pane_id=h["pane_id"], text=text)
            self.call("pane.send_keys", pane_id=h["pane_id"], keys=["enter"])
            return True
        except Exception:
            return False

    def stop(self, h):
        try:
            self.call("pane.close", pane_id=h["pane_id"])
        except Exception:
            pass

    def attach_cmd(self, h):
        return f"herdr   # workspace 'rameness', tab '{h['name']}'"


BACKENDS = {b.name: b for b in (SubprocessBackend, TmuxBackend, ScreenBackend, HerdrBackend)}


def get(name: str, state: Path) -> Backend:
    if name == "auto":
        for n in ("herdr", "tmux", "screen"):
            if BACKENDS[n].available():
                return BACKENDS[n](state)
        return SubprocessBackend(state)
    cls = BACKENDS[name]
    if not cls.available():
        raise RuntimeError(f"session backend {name} is not available here")
    return cls(state)


def available() -> list[str]:
    return [n for n, b in BACKENDS.items() if b.available()]
