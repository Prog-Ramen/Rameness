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

MUTATING = {"bash", "write_file", "edit_file", "edit_lines", "sop_save"}


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
     "input_schema": _schema({"command": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds, default 120, max 600"}}, ["command"])},
    {"name": "read_file", "description": "Read a text file with line numbers. Use offset/limit for large files. "
                                         "Image files (png, jpg, gif, webp) are shown to you as images when your "
                                         "model can see them.",
     "input_schema": _schema({"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"])},
    {"name": "write_file", "description": "Create or overwrite a file with the given content.",
     "input_schema": _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    {"name": "edit_file", "description": "Replace an exact, unique string in a file (set replace_all to replace every occurrence).",
     "input_schema": _schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                              "replace_all": {"type": "boolean"}}, ["path", "old", "new"])},
    {"name": "glob", "description": "Find files by glob pattern (e.g. **/*.js). Returns paths, most recently modified first.",
     "input_schema": _schema({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"])},
    {"name": "web_fetch", "description": (
        "Fetch a web page or file over HTTP(S) and return its text (HTML is converted to plain text). Use it to "
        "research: read documentation, specifications, and how things are usually built or tested."),
     "input_schema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "edit_lines", "description": (
        "Replace a range of lines using the anchors shown by read_file (e.g. start \"12:a3\", end \"20:f1\", "
        "inclusive). Cheaper and safer than edit_file for larger changes: the anchors are checked against the "
        "current file, so a stale read is refused instead of editing the wrong place. new may be empty to delete."),
     "input_schema": _schema({"path": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                              "new": {"type": "string"}}, ["path", "start", "end", "new"])},
    {"name": "grep", "description": "Search file contents with a regex. Returns path:line: text matches.",
     "input_schema": _schema({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}}, ["pattern"])},
    {"name": "recall", "description": "Read a stored artifact (large or evicted tool output) by ref, by line range or regex.",
     "input_schema": _schema({"ref": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
                              "grep": {"type": "string"}}, ["ref"])},
    {"name": "todo_write", "description": (
        "Plan and track multi-step work. Send the whole list every time; each item has content and a status "
        "(pending | in_progress | completed). Use it for any task with three or more steps: write the plan first, "
        "keep one item in_progress, and mark an item completed only once it is done and checked."),
     "input_schema": _schema({"todos": {"type": "array", "items": {"type": "object", "properties": {
         "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
         "required": ["content", "status"]}}}, ["todos"])},
    {"name": "note", "description": (
        "Record durable knowledge about this task, kept even when older conversation is trimmed: a fact you "
        "verified, a decision and why, an open issue, or an artifact you produced (a file, a URL, a result). "
        "Resolve an issue by its id when it is fixed. Keep each note to one or two sentences."),
     "input_schema": _schema({"kind": {"type": "string", "enum": ["fact", "decision", "issue", "artifact"]},
                              "text": {"type": "string"}, "resolve": {"type": "integer",
                                                                      "description": "id of an issue now fixed"}},
                             [])},
    {"name": "delegate", "description": (
        "Hand a self-contained sub-task to a sub-agent that works in a fresh, small context and reports back: "
        "diagnosing one failing component, researching a question, or building one well-specified part. Give it "
        "everything it needs in context (paths, facts, what done means); it does not see this conversation."),
     "input_schema": _schema({"task": {"type": "string", "description": "the sub-task and what done means"},
                              "context": {"type": "string", "description": "facts, file paths and constraints"}},
                             ["task"])},
    {"name": "sop_search", "description": "Search the SOP library for reusable procedures. Matching SOPs become callable tools on your next turn.",
     "input_schema": _schema({"query": {"type": "string"}}, ["query"])},
    {"name": "sop_save", "description": (
        "Save a procedure you just worked out as a reusable SOP script so future tasks don't re-derive it. "
        "Only for deterministic, parameterised operations likely to recur. The script reads JSON args on stdin "
        "and prints a JSON object on stdout. Tests run before it is registered. Include expected output values "
        "for normal and edge cases; checking only the presence of output keys does not verify correctness."),
     "input_schema": _schema({
         "id": {"type": "string", "description": "dotted hierarchical id, e.g. data.parquet_summary"},
         "description": {"type": "string"},
         "inputs": {"type": "object", "description": "JSON schema for the arguments"},
         "script": {"type": "string", "description": "python source"},
         "permissions": {"type": "array", "items": {"type": "string"}},
         "tests": {"type": "array", "items": {"type": "object"},
                   "description": "[{input: {...}, expect: {output_field: expected_value}}]; error cases may use "
                                  "expect_error: true. expect_keys may supplement value assertions."}},
         ["id", "description", "inputs", "script"])},
]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".rameness"}


def _kills_self(command: str) -> str | None:
    """A `pkill`/`killall` in the command that would match this agent's own process or one of its parents (models
    reach for `pkill -f serve` to restart a server, and the agent's command line holds the task text, which says
    'server'). Returns the offending invocation."""
    import shlex
    for m in re.finditer(r"\b(pkill|killall)\b([^;&|\n]*)", command):
        try:
            args = shlex.split(m.group(2))
        except ValueError:
            continue
        full = any(x.startswith("-") and "f" in x.lstrip("-") and not x.startswith("--") for x in args) \
            or "--full" in args
        # patterns only: not options, not redirections like 2>/dev/null or >log
        pats = [x for x in args if not x.startswith("-") and not re.match(r"^\d*[<>]|^&?>", x)]
        if not pats:
            continue
        mine, pid = set(), os.getpid()
        while pid > 1 and len(mine) < 50:                  # this process and its ancestors
            mine.add(pid)
            try:
                pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
        for pat in pats:
            how = ["-f"] if full else ["-x"] if m.group(1) == "killall" else []   # killall: exact process names
            r = subprocess.run(["pgrep", *how, pat], capture_output=True, text=True)
            if mine & {int(x) for x in r.stdout.split() if x.isdigit()}:
                return m.group(0).strip()
    return None


def _whitespace_match(text: str, old: str, new: str) -> tuple[str, int, int] | None:
    """The one place where `old` matches line by line ignoring leading/trailing whitespace: replace those lines with
    `new`, shifted by the indentation difference. None when there is no match or more than one."""
    lines = text.split("\n")
    want = old.strip("\n").split("\n")
    key = [l.strip() for l in want]
    if not any(key):
        return None
    k = len(want)
    hits = [i for i in range(len(lines) - k + 1) if [l.strip() for l in lines[i:i + k]] == key]
    if len(hits) != 1:
        return None
    i = hits[0]
    first = next(j for j, w in enumerate(want) if w.strip())
    ind = lambda l: len(l) - len(l.lstrip())
    delta = ind(lines[i + first]) - ind(want[first])
    body = []
    for l in new.strip("\n").split("\n") if new.strip("\n") else []:
        body.append((" " * delta + l) if delta > 0 else l[min(-delta, ind(l)):] if delta < 0 else l)
    return "\n".join(lines[:i] + body + lines[i + k:]), i + 1, i + k


def _closest(text: str, old: str, anchored: bool, context: int = 2) -> str:
    """Where the file most resembles ``old``, shown as the model would read it, so a failed edit can be retried
    straight away instead of costing a search and a read (the usual cause: whitespace or a line that changed)."""
    import difflib
    lines, want = text.split("\n"), [l.strip() for l in old.strip("\n").split("\n")]
    if not lines or not any(want):
        return ""
    k, best, at = len(want), 0.0, 0
    stripped = [l.strip() for l in lines]
    first = next((w for w in want if w), "")
    starts = [i for i, l in enumerate(stripped) if l and difflib.SequenceMatcher(None, l, first).quick_ratio() > 0.6]
    for i in (starts or range(len(lines)))[:400]:
        i = max(0, i - want.index(first))
        r = difflib.SequenceMatcher(None, "\n".join(stripped[i:i + k]), "\n".join(want)).ratio()
        if r > best:
            best, at = r, i
    if best < 0.5:
        return " (nothing similar in the file)"
    lo, hi = max(0, at - context), min(len(lines), at + k + context)
    show = "\n".join((f"{i + 1}:{anchor(lines[i])}|{lines[i]}" if anchored else f"{i + 1}|{lines[i]}") for i in range(lo, hi))
    how = "edit_lines with these anchors" if anchored else "the exact text below"
    sim = "identical apart from whitespace" if best == 1 else f"{best:.0%} similar"
    return (f"; the closest match ({sim}) is lines {at + 1}-{min(len(lines), at + k)}. "
            f"Retry with {how}:\n{show}")


def anchor(line: str) -> str:
    """Two hex characters from a line's content: with the line number, a handle that goes stale when the line
    changes (so an edit_lines call can never land on the wrong text)."""
    import hashlib
    return hashlib.blake2s(line.rstrip("\r").encode("utf-8", "replace"), digest_size=1).hexdigest()


def _parse_anchor(s: str, last: bool = False) -> tuple[int, str]:
    """'12:a3', or lines pasted as read_file shows them ('12:a3|code...'): models often copy the whole line, or a
    block of lines, as the anchor. Then the start is the first line's anchor and the end the last one's. The hash
    is still checked against the file, so a stale or wrong anchor is refused all the same."""
    found = re.findall(r"(?m)^\s*(\d+):([0-9a-f]{2})(?:\||\s*$)", str(s))
    if not found:
        raise ValueError(f"bad anchor {str(s)[:80]!r}: use the 'line:hash' shown by read_file, e.g. 12:a3")
    n, h = found[-1] if last else found[0]
    return int(n), h
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
IMAGE_MAX_BYTES = 5_000_000     # per image; a screenshot is typically 0.1-1 MB
BASH_MAX_TIMEOUT = 600          # seconds; the default is 120 (both as in Claude Code)
LINE_CHARS = 2000               # a longer output line is cut (as Claude Code's Read): minified or generated code
READ_WHOLE_CHARS = 60000        # read_file without a range: above this, an outline and the opening lines instead
OUTLINE_RX = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function\*?|interface|"
                        r"struct|enum|impl|fn|func|type|module)\b|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*="
                        r"\s*(?:async\s*)?(?:function|\(|class\b|\w+\s*=>)|^\s*(?:#{1,3}\s|\w[\w.]*\s*\(.*\)\s*\{\s*$)")


def cap_line(line: str) -> str:
    if len(line) <= LINE_CHARS:
        return line
    return (f"{line[:LINE_CHARS // 4]} ... [line cut: {len(line)} chars, likely minified or generated; "
            "search it with a narrower pattern instead]")


def cap_lines(text: str) -> str:
    if not text or max(map(len, text.splitlines()), default=0) <= LINE_CHARS:
        return text
    return "\n".join(cap_line(l) for l in text.split("\n"))


def outline(lines: list[str], limit: int = 200) -> list[str]:
    """Definition lines (functions, classes, top-level bindings, headings) with their line numbers."""
    out = [f"{i:6d}\t{l.strip()[:160]}" for i, l in enumerate(lines, 1) if len(l) <= LINE_CHARS and OUTLINE_RX.match(l)]
    return out[:limit] + ([f"... ({len(out) - limit} more definitions)"] if len(out) > limit else [])
FETCH_MAX_BYTES = 5_000_000


def _html_text(html: str) -> str:
    """Readable text from HTML: drops script/style chrome, keeps headings, paragraphs, lists and code."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        skip = {"script", "style", "noscript", "svg", "head"}
        block = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article", "table"}

        def __init__(self):
            super().__init__()
            self.out, self.depth = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in self.skip:
                self.depth += 1
            elif tag in self.block:
                self.out.append("\n")
            if tag in ("h1", "h2", "h3", "h4"):
                self.out.append("#" * int(tag[1]) + " ")
            if tag == "li":
                self.out.append("- ")

        def handle_endtag(self, tag):
            if tag in self.skip and self.depth:
                self.depth -= 1
            elif tag in self.block:
                self.out.append("\n")

        def handle_data(self, data):
            if not self.depth:
                self.out.append(data)

    p = P()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


@dataclass
class Toolbox:
    """Built-in tools. With a non-local ``env`` (ssh / container / exec prefix) every
    tool acts on that environment while the model keeps running wherever it runs."""

    cwd: Path
    approve: Approver
    env: object = None                     # fleet.envs.Environment | None (local)
    line_anchors: bool = False             # read_file shows "N:hh|" line anchors that edit_lines edits by
    edit_fuzzy: bool = False               # edit_file applies an edit whose old text differs only in whitespace
    vision: bool = False                   # the model can see images: read_file on an image shows it
    pending_images: list = field(default_factory=list)   # images read by the current call (take_images)
    branches: object = None                # featurebranch.FeatureBranches during a feature cycle (path redirection)
    handlers: dict[str, Callable[[dict], str]] = field(default_factory=dict)

    def __post_init__(self):
        self.handlers.update({"bash": self.bash, "read_file": self.read_file, "write_file": self.write_file,
                              "edit_file": self.edit_file, "grep": self.grep, "glob": self.glob,
                              "web_fetch": self.web_fetch, "edit_lines": self.edit_lines})

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
        if self.branches is not None:          # a feature cycle: the project directory's paths mean the cycle's copy
            p = self.branches.redirect_path(p)
        # weaker models often write "/file.py" meaning "file.py in the workdir"
        if not p.exists() and (self.cwd / str(path).lstrip("/")).exists():
            return self.cwd / str(path).lstrip("/")
        return p

    def bash(self, a: dict) -> str:
        if self.branches is not None and not self.remote:
            a = {**a, "command": self.branches.redirect_command(a["command"])}
        where = f"[{self.env.id}] " if self.remote else ""
        if not self.approve("bash", where + a["command"]):
            return "DENIED: user did not approve this command"
        if not self.remote and (hit := _kills_self(a["command"])):
            return (f"ERROR: refused: `{hit}` would also kill this agent itself (its own command line matches). "
                    "Stop the process you mean by its PID (find it with `pgrep -af <name>` or `ss -ltnp`), or use "
                    "a narrower pattern.")
        if self.remote:
            r = self.env.run(a["command"], str(self.cwd), timeout=min(a.get("timeout") or 120, BASH_MAX_TIMEOUT))
            return f"exit={r.code}\n{cap_lines(r.out)}" + (f"\n[stderr]\n{cap_lines(r.err)}" if r.err else "")
        try:
            p = subprocess.run(a["command"], shell=True, cwd=self.cwd, capture_output=True, text=True,
                               timeout=min(a.get("timeout") or 120, BASH_MAX_TIMEOUT))
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out"
        return f"exit={p.returncode}\n{cap_lines(p.stdout)}" + (f"\n[stderr]\n{cap_lines(p.stderr)}" if p.stderr else "")

    def take_images(self) -> list[dict]:
        """Images the last tool call read, for the harness to attach to that call's result."""
        imgs, self.pending_images = self.pending_images, []
        return imgs

    def _read_image(self, path: str, media_type: str) -> str:
        if self.remote:
            return f"ERROR: {path} is an image; images can only be viewed in a local working directory"
        data = self._p(path).read_bytes()
        kb = len(data) // 1024
        if not self.vision:
            return (f"[{path} is an image ({kb} KB). This model cannot see images: inspect it with tools instead, "
                    "e.g. its dimensions or pixel statistics.]")
        if len(data) > IMAGE_MAX_BYTES:
            return f"ERROR: {path} is {kb} KB, over the {IMAGE_MAX_BYTES // 1_000_000} MB limit; save a smaller version and read that"
        import base64
        self.pending_images.append({"media_type": media_type, "data": base64.b64encode(data).decode()})
        return f"[image {path} ({kb} KB), shown below]"

    def read_file(self, a: dict) -> str:
        media = IMAGE_TYPES.get(Path(a["path"]).suffix.lower())
        if media:
            return self._read_image(a["path"], media)
        text = self._read(a["path"])
        lines = text.splitlines()
        if "offset" not in a and "limit" not in a and len(text) > READ_WHOLE_CHARS:
            # Too big to be worth holding whole: show its shape and let the agent read the parts it needs.
            head = "\n".join(f"{i:6d}\t{cap_line(l)}" for i, l in enumerate(lines[:150], 1))
            defs = outline(lines)
            return (f"[{a['path']}: {len(lines)} lines, {len(text)} chars - too large to read whole usefully. "
                    "Below: its outline (definitions with line numbers) and the first 150 lines. Read the ranges "
                    "you need with offset/limit, or grep for what you are looking for.]\n\n"
                    + ("== outline ==\n" + "\n".join(defs) + "\n\n" if defs else "")
                    + "== lines 1-150 ==\n" + head)
        off = max(1, a.get("offset", 1))
        lim = a.get("limit", 2000)
        if self.line_anchors:
            body = "\n".join(f"{i}:{anchor(l)}|{cap_line(l)}" for i, l in enumerate(lines[off - 1: off - 1 + lim], off))
        else:
            body = "\n".join(f"{i:6d}\t{cap_line(l)}" for i, l in enumerate(lines[off - 1: off - 1 + lim], off))
        more = f"\n... ({len(lines)} lines total)" if off - 1 + lim < len(lines) else ""
        return body + more

    def edit_lines(self, a: dict) -> str:
        text = self._read(a["path"])
        lines = text.split("\n")
        try:
            (s_no, s_h), (e_no, e_h) = (_parse_anchor(a["start"]), _parse_anchor(a["end"], last=True))
        except ValueError as ex:
            return f"ERROR: {ex}"
        if not (1 <= s_no <= e_no <= len(lines)):
            return f"ERROR: line range {s_no}-{e_no} is outside the file ({len(lines)} lines)"
        stale = [f"{n}:{h}" for n, h in ((s_no, s_h), (e_no, e_h)) if anchor(lines[n - 1]) != h]
        if stale:
            return ("ERROR: anchor " + ", ".join(stale) + " no longer matches the file (it changed since you read "
                    "it). Read the lines again and use the new anchors.")
        if not self.approve("edit_file", f"{a['path']}: lines {s_no}-{e_no}"):
            return "DENIED"
        new = a.get("new", "")
        repl = new.split("\n") if new != "" else []
        self._write(a["path"], "\n".join(lines[:s_no - 1] + repl + lines[e_no:]))
        return f"edited {a['path']}: replaced lines {s_no}-{e_no} with {len(repl)} line(s)"

    def write_file(self, a: dict) -> str:
        if not self.approve("write_file", a["path"]):
            return "DENIED"
        self._write(a["path"], a["content"])
        return f"wrote {len(a['content'])} chars to {a['path']}"

    def edit_file(self, a: dict) -> str:
        text = self._read(a["path"])
        n = text.count(a["old"])
        if n == 0:
            if self.edit_fuzzy and (fixed := _whitespace_match(text, a["old"], a["new"])):
                if not self.approve("edit_file", f"{a['path']}: -{a['old'][:200]!r} +{a['new'][:200]!r}"):
                    return "DENIED"
                self._write(a["path"], fixed[0])
                return (f"edited {a['path']} (1 replacement; the old text matched lines {fixed[1]}-{fixed[2]} apart "
                        "from whitespace, and the new text was re-indented to match)")
            return "ERROR: old string not found" + _closest(text, a["old"], self.line_anchors)
        if n > 1 and not a.get("replace_all"):
            return f"ERROR: old string occurs {n} times; make it unique or set replace_all"
        if not self.approve("edit_file", f"{a['path']}: -{a['old'][:200]!r} +{a['new'][:200]!r}"):
            return "DENIED"
        self._write(a["path"], text.replace(a["old"], a["new"]) if a.get("replace_all")
                    else text.replace(a["old"], a["new"], 1))
        return f"edited {a['path']} ({n if a.get('replace_all') else 1} replacement(s))"

    def glob(self, a: dict) -> str:
        if self.remote:
            import shlex
            r = self.env.run(f"find {shlex.quote(a.get('path') or '.')} -path {shlex.quote('*/' + a['pattern'].lstrip('*/'))} "
                             "-type f -not -path '*/node_modules/*' -not -path '*/.git/*' | head -100", str(self.cwd))
            return r.out or "(no matches)"
        base = self._p(a.get("path") or ".")
        hits = [f for f in base.glob(a["pattern"]) if f.is_file() and not SKIP_DIRS & set(f.parts)]
        hits.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        rel = [str(f.relative_to(self.cwd)) if f.is_relative_to(self.cwd) else str(f) for f in hits[:100]]
        if not rel:
            return "(no matches)"
        return "\n".join(rel) + (f"\n... ({len(hits) - 100} more)" if len(hits) > 100 else "")

    def web_fetch(self, a: dict) -> str:
        import urllib.request
        url = a["url"]
        if not url.startswith(("http://", "https://")):
            return "ERROR: only http(s) URLs"
        if not self.approve("web_fetch", url):
            return "DENIED: user did not approve this fetch"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (rameness web_fetch)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(FETCH_MAX_BYTES)
            ctype = r.headers.get("Content-Type", "")
            final = r.geturl()
            charset = r.headers.get_content_charset() or "utf-8"
        text = body.decode(charset, errors="replace")
        if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            text = _html_text(text)
        return f"URL: {final}\nContent-Type: {ctype}\n\n{text}"

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
