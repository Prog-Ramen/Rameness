"""What software an SOP needs beyond Python and a POSIX shell.

Deterministic, no model: Python imports outside the standard library, and the programs its shell
script or its ``subprocess`` calls run. The result is recorded on every SOP (built-in, registry or
private) as ``requirements`` and shown with it, so people and the agent know what may need
installing. It lists; it never installs anything.
"""

from __future__ import annotations

import ast
import re
import shlex
import sys
from pathlib import Path

# Programs a POSIX system has without installing anything (plus the Python that runs Rameness).
BASE_COMMANDS = {
    "sh", "bash", "env", "echo", "printf", "cat", "ls", "cp", "mv", "rm", "mkdir", "rmdir", "touch", "ln",
    "chmod", "head", "tail", "cut", "tr", "sort", "uniq", "wc", "grep", "egrep", "sed", "awk", "find",
    "xargs", "tee", "date", "sleep", "test", "[", "true", "false", "basename", "dirname", "pwd", "cd",
    "stat", "du", "df", "diff", "cmp", "tar", "gzip", "gunzip", "od", "expr", "seq", "realpath",
    "readlink", "mktemp", "kill", "ps", "uname", "id", "whoami", "hostname", "which", "command", "type",
    "exit", "export", "set", "unset", "read", "shift", "local", "return", "if", "then", "else", "fi",
    "for", "do", "done", "while", "case", "esac", "python", "python3",
    "git",                                  # Rameness itself needs git (feature branches, registry PRs)
}
SHELL_WORDS = {"&&", "||", "|", ";", "!", "{", "}", "(", ")"}
# Import names whose package is installed under another name.
PACKAGE_NAMES = {"yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "sklearn": "scikit-learn",
                 "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
                 "magic": "python-magic", "serial": "pyserial", "Crypto": "pycryptodome", "jwt": "pyjwt",
                 "docx": "python-docx", "pptx": "python-pptx", "attr": "attrs", "google": "google-api-core"}


def _python_imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def _commands_in_shell(text: str) -> set[str]:
    """First word of every simple command in a shell script (after env assignments)."""
    found: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            words = shlex.split(line, posix=True)
        except ValueError:
            words = line.split()
        expect = True
        for w in words:
            if w in SHELL_WORDS or w.endswith(";"):
                expect = True
                continue
            if expect:
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):     # VAR=value prefix
                    continue
                if re.match(r"^[A-Za-z0-9_.+-]+$", w):
                    found.add(w)
                expect = False
    return found


def _commands_in_python(source: str) -> set[str]:
    """Programs a Python script runs: subprocess / os calls with a literal command."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if name not in {"run", "call", "check_call", "check_output", "Popen", "system", "popen", "which"}:
            continue
        a = node.args[0]
        if isinstance(a, (ast.List, ast.Tuple)) and a.elts and isinstance(a.elts[0], ast.Constant) \
                and isinstance(a.elts[0].value, str):
            found.add(Path(a.elts[0].value).name)
        elif isinstance(a, ast.Constant) and isinstance(a.value, str):
            found |= {Path(c).name for c in _commands_in_shell(a.value)} if name != "which" else {a.value}
    return found


def requirements(sop_dir: Path) -> dict:
    """``{"python": [...], "commands": [...]}`` beyond the standard library and a POSIX shell."""
    stdlib = set(sys.stdlib_module_names)
    py: set[str] = set()
    cmds: set[str] = set()
    local = {p.stem for p in sop_dir.glob("*.py")}
    for f in sorted(sop_dir.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        text = f.read_text(errors="replace")
        if f.suffix == ".py":
            py |= _python_imports(text)
            cmds |= _commands_in_python(text)
        elif f.suffix == ".sh" or text.startswith("#!/bin/sh") or text.startswith("#!/usr/bin/env bash"):
            cmds |= _commands_in_shell(text)
    py = {PACKAGE_NAMES.get(m, m) for m in py if m not in stdlib and m not in local and m != "rameness"}
    cmds = {c for c in cmds if c not in BASE_COMMANDS}
    return {"python": sorted(py), "commands": sorted(cmds)}


def describe(req: dict) -> str:
    """One line for people and for the agent: what may need installing."""
    parts = []
    if req.get("python"):
        parts.append("Python packages: " + ", ".join(req["python"]))
    if req.get("commands"):
        parts.append("programs: " + ", ".join(req["commands"]))
    return "May need " + "; ".join(parts) + "." if parts else ""
