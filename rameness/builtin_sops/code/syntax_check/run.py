"""SOP code.syntax_check: syntax diagnostics with the toolchains already on the machine.

Weaker models make exact-detail slips (a stray bracket, a missing semicolon) and then spend many turns
tracing them through indirect symptoms. Checking a file as soon as it is written turns that into a
one-turn fix, the way an editor underlines an error as you type.

Every check is a shell command from the language's own toolchain in its syntax-only mode (``bash -n``,
``perl -c``, ``cc -fsyntax-only``, ``gofmt -e``, ``ruby -c``, ``php -l``, ``node --check``...). Nothing
extra is installed: a checker whose tool is missing is skipped. ``diagnostics_commands`` in the config adds
languages or overrides these, e.g. ``{".kt": "kotlinc -script {f}", ".rs": null}``.

Rameness runs it as a lifecycle hook after write_file / edit_file, and after bash on the files the command
created or changed. Input {"paths": [...], "commands": {...}}; output {"errors": [...], "report": "..."},
where an empty report means there is nothing to fix.
"""

from __future__ import annotations

import json
import os
import sys
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

# extension -> (probe, command). The probe decides whether the checker exists here (default: its first
# word is on PATH); {f} is the file. Commands exit non-zero and print the error on a syntax problem.
PY = "python3 -c"
CHECKERS: dict[str, tuple[str | None, str]] = {
    ".py": (None, "python3 -c \"import ast,sys\ntry: ast.parse(open(sys.argv[1],encoding='utf-8',errors='replace').read())\n"
                  "except SyntaxError as e: sys.exit('%s:%s: SyntaxError: %s' % (sys.argv[1], e.lineno, e.msg))\" {f}"),
    ".json": (None, "python3 -m json.tool {f} > /dev/null"),
    ".toml": (f"{PY} 'import tomllib'", f"{PY} \"import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))\" {{f}}"),
    ".xml": (None, f"{PY} \"import sys,xml.dom.minidom as m; m.parse(sys.argv[1])\" {{f}}"),
    ".svg": (None, f"{PY} \"import sys,xml.dom.minidom as m; m.parse(sys.argv[1])\" {{f}}"),
    ".yaml": (f"{PY} 'import yaml'", f"{PY} \"import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))\" {{f}}"),
    ".yml": (f"{PY} 'import yaml'", f"{PY} \"import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))\" {{f}}"),
    ".sh": (None, "bash -n {f}"),
    ".bash": (None, "bash -n {f}"),
    ".zsh": (None, "zsh -n {f}"),
    ".pl": (None, "perl -c {f} 2>&1 >/dev/null | grep -v 'syntax OK' >&2; exit ${PIPESTATUS[0]}"),
    ".pm": (None, "perl -c {f} 2>&1 >/dev/null | grep -v 'syntax OK' >&2; exit ${PIPESTATUS[0]}"),
    ".c": (None, "cc -fsyntax-only -w {f}"),
    ".h": (None, "cc -fsyntax-only -w -x c {f}"),
    ".cc": (None, "c++ -fsyntax-only -w {f}"),
    ".cpp": (None, "c++ -fsyntax-only -w {f}"),
    ".cxx": (None, "c++ -fsyntax-only -w {f}"),
    ".hpp": (None, "c++ -fsyntax-only -w -x c++ {f}"),
    ".go": (None, "gofmt -e -l {f} > /dev/null"),
    ".rs": (None, "rustfmt --edition 2021 --emit stdout {f} > /dev/null"),
    ".rb": (None, "ruby -c {f} > /dev/null"),
    ".php": (None, "php -l {f} > /dev/null"),
    ".lua": (None, "luac -p {f}"),
    ".swift": (None, "swiftc -parse {f}"),
    ".ts": ("node -e \"require('module').stripTypeScriptTypes\"", "node --no-warnings -e \"{ts}\" {f}"),
    ".mts": ("node -e \"require('module').stripTypeScriptTypes\"", "node --no-warnings -e \"{ts}\" {f}"),
    ".cts": ("node -e \"require('module').stripTypeScriptTypes\"", "node --no-warnings -e \"{ts}\" {f}"),
}
# TypeScript: node's built-in type stripper parses the file (type syntax errors), then the stripped JS,
# which keeps every line and column, goes through node --check (JS syntax errors, with positions).
_TS = ("const fs=require('fs'),m=require('module'),cp=require('child_process'),os=require('os'),p=require('path');"
       "const f=process.argv[1];let js;try{js=m.stripTypeScriptTypes(fs.readFileSync(f,'utf8'),{mode:'strip'})}"
       "catch(e){console.error(f+': '+(e.message||e).split('\\\\n')[0]);process.exit(1)}"
       "const t=p.join(os.tmpdir(),'rmn-'+process.pid+'.mjs');fs.writeFileSync(t,js);"
       "const r=cp.spawnSync(process.execPath,['--check',t],{encoding:'utf8'});fs.unlinkSync(t);"
       "if(r.status){console.error(r.stderr.split(t).join(f));process.exit(1)}")
CHECKABLE = set(CHECKERS) | {".js", ".mjs", ".cjs", ".html", ".htm"}

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_JS_TYPES = {"", "text/javascript", "application/javascript", "module", "text/ecmascript"}
_MODULE = re.compile(r"^\s*(import\s[\w{*'\"]|import\(|export\s)", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_probe_cache: dict[str, bool] = {}


def _available(probe: str | None, cmd: str) -> bool:
    key = probe or cmd.split()[0]
    if key not in _probe_cache:
        if probe is None:
            _probe_cache[key] = shutil.which(key) is not None
        else:
            try:
                _probe_cache[key] = subprocess.run(["bash", "-c", probe + " >/dev/null 2>&1"],
                                                   timeout=15).returncode == 0
            except subprocess.TimeoutExpired:
                _probe_cache[key] = False
    return _probe_cache[key]


def _run(cmd: str, path: str) -> str | None:
    """The checker's complaint, or None when the file is fine (or the checker could not run)."""
    try:
        p = subprocess.run(["bash", "-c", cmd.replace("{ts}", _TS).replace("{f}", shlex.quote(path))],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if p.returncode == 0:
        return None
    lines = [ln.rstrip() for ln in _ANSI.sub("", p.stderr + p.stdout).splitlines()
             if ln.strip() and not ln.lstrip().startswith("at ") and "Node.js v" not in ln
             and "--trace-warnings" not in ln and "ExperimentalWarning" not in ln]
    return "\n".join(lines[:8]) or f"exit {p.returncode}"


def _node_js(src: str, module: bool) -> str | None:
    if not shutil.which("node"):
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".mjs" if module else ".cjs", delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        return _run("node --check {f}", tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def check(path: Path, extra: dict | None = None) -> list[str]:
    """Syntax problems in ``path`` as readable text; empty when fine or when no checker exists here."""
    ext = path.suffix.lower()
    table = {**CHECKERS, **{k.lower(): ((None, v) if isinstance(v, str) else v) for k, v in (extra or {}).items()}}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    if ext in (".js", ".mjs", ".cjs") and ext not in (extra or {}):
        err = _node_js(text, ext == ".mjs" or (ext == ".js" and bool(_MODULE.search(text))))
        return [err.replace(err.split(":")[0], path.name, 1) if err else ""] if err else []
    if ext in (".html", ".htm") and ext not in (extra or {}):
        out = []
        for i, m in enumerate(_SCRIPT.finditer(text), 1):
            attrs, body = m.group(1), m.group(2)
            t = re.search(r"\btype\s*=\s*[\"']?([\w/+-]+)", attrs, re.I)
            if re.search(r"\bsrc\s*=", attrs, re.I) or (t.group(1).lower() if t else "") not in _JS_TYPES:
                continue                       # external scripts, shaders, JSON, import maps, templates
            err = _node_js(body, bool(t) and t.group(1).lower() == "module")
            if err:
                first = text.count("\n", 0, m.start(2))          # page lines before the script body
                lm = re.search(r":(\d+)\n", err)
                page_line = first + int(lm.group(1)) if lm else first + 1
                msg = err.split("\n", 1)[1] if "\n" in err else err
                out.append(f"{path.name}:{page_line} (inline script {i}):\n{msg}")
        return out
    spec = table.get(ext)
    if not spec:
        return []
    probe, cmd = spec
    if not _available(probe, cmd):
        return []
    err = _run(cmd, str(path))
    return [err] if err else []


def report(*paths: Path, extra: dict | None = None) -> str:
    """Text to append to a tool result: empty when there is nothing to fix."""
    # every complaint starts with the file it is about, whatever the checker printed
    errs = [e if e.startswith((str(p), p.name)) else f"{p.name}: {e}" for p in paths for e in check(p, extra)]
    if not errs:
        return ""
    return ("\n[diagnostics] syntax errors in the file(s) as they are now; fix these before anything else:\n"
            + "\n".join("  " + e.replace("\n", "\n  ") for e in errs))


if __name__ == "__main__":
    a = json.load(sys.stdin)
    paths = [Path(p) for p in a.get("paths") or [] if Path(p).is_file()]
    extra = a.get("commands") or {}
    errs = [e if e.startswith((str(p), p.name)) else f"{p.name}: {e}" for p in paths for e in check(p, extra)]
    rep = report(*paths, extra=extra) if errs else ""
    print(json.dumps({"errors": errs, "report": rep.lstrip()}))
