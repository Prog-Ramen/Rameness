"""Built-in agent tools (the claude-code / codex core set) plus the SOP tools.

The tool set handed to the model is dynamic: the router pre-selects SOP tools,
``sop_search`` can add more mid-task, and ``sop_save`` lets the model turn a
procedure it just worked out into a validated tool on the spot.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MUTATING = {"bash", "write_file", "edit_file", "sop_save"}


class Approver:
    """Permission gate. mode: ask | auto | readonly."""

    def __init__(self, mode: str = "ask", prompt: Callable[[str], str] | None = None):
        self.mode = mode
        self.prompt = prompt or input
        self.always: set[str] = set()

    def __call__(self, action: str, detail: str) -> bool:
        if self.mode == "auto" or action in self.always:
            return True
        if self.mode == "readonly":
            return False
        try:
            ans = self.prompt(f"\n[rameness] allow {action}: {detail[:500]}\n  [y]es / [n]o / [a]lways > ").strip().lower()
        except EOFError:
            return False
        if ans == "a":
            self.always.add(action)
        return ans in ("y", "yes", "a")


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


BUILTIN_SCHEMAS = [
    {"name": "bash", "description": "Run a shell command in the project directory. Returns exit code, stdout and stderr.",
     "input_schema": _schema({"command": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds, default 120"}}, ["command"])},
    {"name": "read_file", "description": "Read a text file with line numbers. Use offset/limit for large files.",
     "input_schema": _schema({"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"])},
    {"name": "write_file", "description": "Create or overwrite a file with the given content.",
     "input_schema": _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    {"name": "edit_file", "description": "Replace an exact, unique string in a file (set replace_all to replace every occurrence).",
     "input_schema": _schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                              "replace_all": {"type": "boolean"}}, ["path", "old", "new"])},
    {"name": "grep", "description": "Search file contents with a regex. Returns path:line: text matches.",
     "input_schema": _schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}}, ["pattern"])},
    {"name": "recall", "description": "Read a stored artifact (large or evicted tool output) by ref, by line range or regex.",
     "input_schema": _schema({"ref": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
                              "grep": {"type": "string"}}, ["ref"])},
    {"name": "sop_search", "description": "Search the SOP library for reusable procedures. Matching SOPs become callable tools on your next turn.",
     "input_schema": _schema({"query": {"type": "string"}}, ["query"])},
    {"name": "sop_save", "description": (
        "Save a procedure you just worked out as a reusable SOP script so future tasks don't re-derive it. "
        "Only for deterministic, parameterised operations likely to recur. The script reads JSON args on stdin "
        "and prints a JSON object on stdout. Tests run before it is registered."),
     "input_schema": _schema({
         "id": {"type": "string", "description": "dotted hierarchical id, e.g. data.parquet_summary"},
         "description": {"type": "string"},
         "inputs": {"type": "object", "description": "JSON schema for the arguments"},
         "script": {"type": "string", "description": "python source"},
         "permissions": {"type": "array", "items": {"type": "string"}},
         "tests": {"type": "array", "items": {"type": "object"},
                   "description": "[{input: {...}, expect_keys: [...]}]"}},
         ["id", "description", "inputs", "script"])},
]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".rameness"}


@dataclass
class Toolbox:
    """Built-in tools. With a non-local ``env`` (ssh / container / exec prefix) every
    tool acts on that environment while the model keeps running wherever it runs."""

    cwd: Path
    approve: Approver
    env: object = None                     # fleet.envs.Environment | None (local)
    handlers: dict[str, Callable[[dict], str]] = field(default_factory=dict)

    def __post_init__(self):
        self.handlers.update({"bash": self.bash, "read_file": self.read_file, "write_file": self.write_file,
                              "edit_file": self.edit_file, "grep": self.grep})

    @property
    def remote(self) -> bool:
        return self.env is not None and getattr(self.env, "kind", "local") != "local"

    def _read(self, path: str) -> str:
        return self.env.read_text(path, str(self.cwd)) if self.remote else self._p(path).read_text(errors="replace")

    def _write(self, path: str, text: str) -> None:
        if self.remote:
            self.env.write_text(path, text, str(self.cwd))
            return
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def _p(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            return self.cwd / p
        # weaker models often write "/file.py" meaning "file.py in the workdir"
        if not p.exists() and (self.cwd / path.lstrip("/")).exists():
            return self.cwd / path.lstrip("/")
        return p

    def bash(self, a: dict) -> str:
        where = f"[{self.env.id}] " if self.remote else ""
        if not self.approve("bash", where + a["command"]):
            return "DENIED: user did not approve this command"
        if self.remote:
            r = self.env.run(a["command"], str(self.cwd), timeout=a.get("timeout", 120))
            return f"exit={r.code}\n{r.out}" + (f"\n[stderr]\n{r.err}" if r.err else "")
        try:
            p = subprocess.run(a["command"], shell=True, cwd=self.cwd, capture_output=True, text=True,
                               timeout=a.get("timeout", 120))
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out"
        return f"exit={p.returncode}\n{p.stdout}" + (f"\n[stderr]\n{p.stderr}" if p.stderr else "")

    def read_file(self, a: dict) -> str:
        lines = self._read(a["path"]).splitlines()
        off = max(1, a.get("offset", 1))
        lim = a.get("limit", 2000)
        body = "\n".join(f"{i:6d}\t{l}" for i, l in enumerate(lines[off - 1: off - 1 + lim], off))
        more = f"\n... ({len(lines)} lines total)" if off - 1 + lim < len(lines) else ""
        return body + more

    def write_file(self, a: dict) -> str:
        if not self.approve("write_file", a["path"]):
            return "DENIED"
        self._write(a["path"], a["content"])
        return f"wrote {len(a['content'])} chars to {a['path']}"

    def edit_file(self, a: dict) -> str:
        text = self._read(a["path"])
        n = text.count(a["old"])
        if n == 0:
            return "ERROR: old string not found"
        if n > 1 and not a.get("replace_all"):
            return f"ERROR: old string occurs {n} times; make it unique or set replace_all"
        if not self.approve("edit_file", f"{a['path']}: -{a['old'][:200]!r} +{a['new'][:200]!r}"):
            return "DENIED"
        self._write(a["path"], text.replace(a["old"], a["new"]) if a.get("replace_all")
                    else text.replace(a["old"], a["new"], 1))
        return f"edited {a['path']} ({n if a.get('replace_all') else 1} replacement(s))"

    def grep(self, a: dict) -> str:
        if self.remote:
            import shlex
            inc = f" --include={shlex.quote(a['glob'])}" if a.get("glob") else ""
            excl = " ".join(f"--exclude-dir={d}" for d in SKIP_DIRS)
            r = self.env.run(f"grep -rnIE {excl}{inc} {shlex.quote(a['pattern'])} {shlex.quote(a.get('path', '.'))} | head -300",
                             str(self.cwd))
            return r.out or "(no matches)"
        rx = re.compile(a["pattern"])
        root = self._p(a.get("path", "."))
        files = [root] if root.is_file() else None
        hits = []
        if files is None:
            files = []
            for d, dirs, fs in os.walk(root):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
                files += [Path(d) / f for f in fs if not a.get("glob") or fnmatch.fnmatch(f, a["glob"])]
        for f in files:
            try:
                for i, line in enumerate(f.read_text(errors="strict").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.cwd) if f.is_relative_to(self.cwd) else f}:{i}: {line[:300]}")
                        if len(hits) >= 300:
                            return "\n".join(hits) + "\n... (truncated)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) or "(no matches)"

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        h = self.handlers.get(name)
        if not h:
            return f"ERROR: unknown tool {name}", True
        try:
            out = h(args)
            return (out if isinstance(out, str) else json.dumps(out, indent=1, default=str)), False
        except Exception as e:  # tool errors go back to the model, not up the stack
            return f"ERROR: {type(e).__name__}: {e}", True
