"""Execution environments: where an agent's tools run, independent of where its model runs.

    {"id": "gpu-box", "kind": "ssh", "host": "me@10.0.0.7", "workdir": "/srv/work",
     "tags": ["gpu", "cuda"], "ssh_opts": ["-p", "2222"]}
    {"id": "sandbox", "kind": "container", "engine": "docker", "container": "dev", "workdir": "/work"}
    {"id": "k8s-job", "kind": "exec", "prefix": ["kubectl", "exec", "-i", "pod/x", "--"], "workdir": "/w"}

``local`` always exists. ``probe()`` reports CPUs, RAM, GPUs and tools so the JEV
can match work (e.g. "fine-tune", "cuda") to an environment with the power for it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PROBE = r"""
cpus=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 0)
mem=$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)
memfree=$(awk '/MemAvailable/{printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)
load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)
gpus=$(nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | tr '\n' ';')
tools=""; for t in python3 git docker node cargo go nvcc uv; do command -v $t >/dev/null 2>&1 && tools="$tools $t"; done
echo "{\"cpus\": $cpus, \"mem_gb\": $mem, \"mem_free_gb\": $memfree, \"load\": \"$load\", \"os\": \"$(uname -sm)\", \"gpus_raw\": \"$gpus\", \"tools\": \"$tools\"}"
"""


@dataclass
class Proc:
    code: int
    out: str
    err: str


@dataclass
class Environment:
    id: str
    kind: str = "local"
    workdir: str = "."
    tags: list[str] = field(default_factory=list)
    description: str = ""
    host: str = ""
    ssh_opts: list[str] = field(default_factory=list)
    engine: str = "docker"
    container: str = ""
    prefix: list[str] = field(default_factory=list)
    max_agents: int = 4
    info: dict = field(default_factory=dict)

    # ---- launching

    def argv(self, cmd: str, cwd: str | None = None) -> list[str]:
        """argv that runs shell command ``cmd`` inside this environment."""
        cwd = cwd or self.workdir
        inner = f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd
        if self.kind == "local":
            return ["bash", "-c", inner]
        if self.kind == "ssh":
            return ["ssh", "-o", "BatchMode=yes", *self.ssh_opts, self.host, "bash -lc " + shlex.quote(inner)]
        if self.kind == "container":
            return [self.engine, "exec", "-i", self.container, "bash", "-lc", inner]
        if self.kind == "exec":
            return [*self.prefix, "bash", "-lc", inner]
        raise ValueError(f"unknown environment kind {self.kind}")

    def interactive_argv(self, argv: list[str], cwd: str | None = None) -> list[str]:
        """argv for a TTY program (a CLI agent) running inside the environment."""
        cmd = " ".join(shlex.quote(a) for a in argv)
        if self.kind == "ssh":
            inner = f"cd {shlex.quote(cwd or self.workdir)} && {cmd}"
            return ["ssh", "-t", *self.ssh_opts, self.host, "bash -lc " + shlex.quote(inner)]
        if self.kind == "container":
            return [self.engine, "exec", "-it", "-w", cwd or self.workdir, self.container, *argv]
        return self.argv(cmd, cwd)

    def run(self, cmd: str, cwd: str | None = None, timeout: int = 120, input: str | None = None) -> Proc:
        try:
            p = subprocess.run(self.argv(cmd, cwd), input=input, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Proc(124, "", f"timed out after {timeout}s")
        except FileNotFoundError as e:
            return Proc(127, "", str(e))
        return Proc(p.returncode, p.stdout, p.stderr)

    # ---- files (local uses the filesystem directly; everything else goes through run)

    def path(self, p: str, cwd: str | None = None) -> str:
        if p.startswith("/"):
            return p
        return os.path.join(cwd or self.workdir, p)

    def read_text(self, p: str, cwd: str | None = None) -> str:
        full = self.path(p, cwd)
        if self.kind == "local":
            return Path(full).read_text(errors="replace")
        r = self.run(f"cat {shlex.quote(full)}")
        if r.code:
            raise FileNotFoundError(r.err.strip() or full)
        return r.out

    def write_text(self, p: str, text: str, cwd: str | None = None) -> None:
        full = self.path(p, cwd)
        if self.kind == "local":
            Path(full).parent.mkdir(parents=True, exist_ok=True)
            Path(full).write_text(text)
            return
        q = shlex.quote(full)
        r = self.run(f"mkdir -p $(dirname {q}) && cat > {q}", input=text)
        if r.code:
            raise OSError(r.err.strip())

    def put_dir(self, local: Path, remote: str) -> None:
        for f in local.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                self.write_text(os.path.join(remote, str(f.relative_to(local))), f.read_text())

    # ---- capabilities

    def probe(self, timeout: int = 15) -> dict:
        r = self.run(PROBE, cwd="/", timeout=timeout)
        if r.code:
            self.info = {"reachable": False, "error": (r.err or r.out).strip()[:300]}
            return self.info
        try:
            d = json.loads(r.out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            self.info = {"reachable": False, "error": r.out[:300]}
            return self.info
        gpus = []
        for g in filter(None, d.pop("gpus_raw", "").split(";")):
            parts = [x.strip() for x in g.split(",")]
            if len(parts) >= 4:
                gpus.append({"name": parts[0], "mem_gb": round(int(parts[1]) / 1024, 1),
                             "used_gb": round(int(parts[2]) / 1024, 1), "util": int(parts[3])})
        d["gpus"] = gpus
        d["tools"] = d.get("tools", "").split()
        d["reachable"] = True
        self.info = d
        return d

    @property
    def text(self) -> str:
        """Capability description the JEV scores against."""
        i = self.info
        g = ", ".join(f"{x['name']} {x['mem_gb']}GB" for x in i.get("gpus", []))
        power = f"{i.get('cpus', '?')} cpus {i.get('mem_gb', '?')}GB ram"
        gpu = f"gpu cuda accelerator {g}" if g else "no gpu cpu only"
        return f"{self.id} {self.kind} {self.description} {' '.join(self.tags)} {power} {gpu} {' '.join(i.get('tools', []))}"

    @property
    def has_gpu(self) -> bool:
        return bool(self.info.get("gpus")) or "gpu" in self.tags

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.kind, "workdir": self.workdir, "tags": self.tags,
                "description": self.description, "host": self.host or self.container, "max_agents": self.max_agents,
                "info": self.info}


def from_config(d: dict) -> Environment:
    known = set(Environment.__dataclass_fields__)
    return Environment(**{k: v for k, v in d.items() if k in known})


def local(workdir: str) -> Environment:
    return Environment("local", "local", workdir, description="this machine")
