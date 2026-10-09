"""Hierarchical SOP library, JEV-driven activation, and the SOP executor.

On disk an SOP library is a directory tree::

    http/_node.json          category: {"description", "keywords", "requires": [...]}
    http/get/sop.json        leaf SOP (see ``SOP``)
    http/get/run.py          implementation: JSON args on stdin -> JSON result on stdout

Several roots are layered, lowest precedence first::

    builtin package  ->  ~/.rameness/public/<pkg>  ->  ~/.rameness/sops  ->  ./.rameness/sops
    (public, generic)                                  (private: user / organization)

A private SOP with the same id overrides a public one, so an organization can
specialise ``customer_data.retrieve`` without forking the public package.

SOP kinds:

* ``script``    - deterministic, parameterised; executable with no LLM at all
* ``composite`` - a sequence of other SOPs with ``${input.x}`` / ``${steps.N.y}`` templating
* ``skill``     - procedural instructions for the model (too fuzzy to script)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .jev import Jev, Option

BUILTIN_ROOT = Path(__file__).parent / "builtin_sops"


# --------------------------------------------------------------------------- model

@dataclass
class SOP:
    id: str
    path: Path
    description: str
    kind: str = "script"
    entry: str = "run.py"
    inputs: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    outputs: dict = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    instructions: str = ""
    tests: list[dict] = field(default_factory=list)
    status: str = "validated"          # validated | candidate
    scope: str = "public"              # public | private
    version: str = "1.0.0"
    origin: dict = field(default_factory=dict)
    visibility: str = "private"        # private | shareable (JEV-classified; only shareable can be proposed)
    classified: dict = field(default_factory=dict)
    requirements: dict = field(default_factory=dict)   # {"python": [...], "commands": [...]} it may need (deps.py)
    destination: str = ""              # where a shareable SOP is proposed: builtin (Rameness) | registry (RamenSOPs)

    @classmethod
    def load(cls, path: Path, sop_id: str, scope: str) -> "SOP":
        d = json.loads((path / "sop.json").read_text())
        known = set(cls.__dataclass_fields__) - {"id", "path", "scope"}
        s = cls(id=d.get("id", sop_id), path=path, scope=scope,
                **{k: v for k, v in d.items() if k in known})
        if s.kind == "skill" and not s.instructions and (path / "SKILL.md").exists():
            s.instructions = (path / "SKILL.md").read_text()
        return s

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("path", "scope")}
        return {k: v for k, v in d.items() if v not in ([], {}, "")}

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "sop.json").write_text(json.dumps(self.to_json(), indent=2) + "\n")

    @property
    def text(self) -> str:
        """What the JEV scores against."""
        return f"{self.id.replace('.', ' ')} {self.description} {' '.join(self.keywords)}"

    @property
    def desc(self) -> str:
        """What a decision model reads as this SOP's criterion."""
        return self.description or self.id

    def digest(self) -> str:
        """What JEV reads when it evaluates this SOP itself: its description and its code
        (comments and blank lines dropped), not the rest of the library or the org profile."""
        if self.kind == "skill":
            body = self.instructions
        elif self.kind == "composite":
            body = json.dumps(self.steps)
        else:
            src = self.path / self.entry
            body = src.read_text(errors="replace") if src.is_file() else ""
            body = "\n".join(ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#"))
        return f"SOP {self.id}: {self.description}\n{body}"

    @property
    def tool_name(self) -> str:
        return "sop_" + re.sub(r"[^a-zA-Z0-9_]", "_", self.id.replace(".", "__"))[:60]

    def tool_schema(self, uses: int = 0) -> dict:
        desc = f"[SOP {self.id}] {self.description}"
        if self.status == "validated" and self.tests:
            desc += (f" Tested: its {len(self.tests)} tests pass" + (f", used successfully {uses} times" if uses else "")
                     + "; its output is verified, no need to recompute it.")
        if self.outputs:
            desc += f" Returns: {json.dumps(self.outputs)[:300]}"
        if self.requirements:
            from .deps import describe
            desc += (" " + describe(self.requirements)).rstrip()
        return {"name": self.tool_name, "description": desc, "input_schema": self.inputs}

    def interface(self) -> str:
        props = self.inputs.get("properties", {})
        req = set(self.inputs.get("required", []))
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        return f"{self.id}({args}) - {self.description}"


@dataclass
class Requirement:
    name: str
    hints: list[str] = field(default_factory=list)
    question: str = ""


@dataclass
class Node:
    id: str                              # "" for root, "http", "http.get", ...
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    requires: list[Requirement] = field(default_factory=list)
    children: dict[str, "Node"] = field(default_factory=dict)
    sop: SOP | None = None

    @property
    def text(self) -> str:
        if self.sop:
            return self.sop.text
        kids = " ".join(self.children)
        return f"{self.id.split('.')[-1]} {self.description} {' '.join(self.keywords)} {kids}"

    @property
    def desc(self) -> str:
        if self.sop:
            return self.sop.desc
        kids = ", ".join(self.children)
        return f"{self.description or self.id.split('.')[-1]}" + (f" (covers: {kids})" if kids else "")

    def walk(self):
        yield self
        for c in self.children.values():
            yield from c.walk()


def keep_out_of_git(root: Path) -> None:
    """Private SOPs (code and tests) never reach GitHub: a private folder inside a git work tree gets a
    .gitignore that ignores everything in it, unless it already has one."""
    root = Path(root)
    if (root / ".gitignore").exists():
        return
    if any((p / ".git").exists() for p in [root, *root.parents]):
        root.mkdir(parents=True, exist_ok=True)
        (root / ".gitignore").write_text("# private SOPs and their tests stay on this machine (rameness)\n*\n")


# --------------------------------------------------------------------------- library

class Library:
    def __init__(self, roots: list[tuple[Path, str]], stats_path: Path | None = None):
        self.roots = roots
        self.stats_path = stats_path
        self.stats: dict[str, dict] = json.loads(stats_path.read_text()) if stats_path and stats_path.exists() else {}
        self.reload()

    @classmethod
    def default(cls, cwd: Path, home: Path, private: list | None = None, save_to=None) -> "Library":
        """Built-in, then registry packages, then private folders: the user's, the project's, then any the user
        chose (``sops.private``, RAMENESS_SOPS, --sops), e.g. one per use case or session. Later ones win."""
        roots: list[tuple[Path, str]] = [(BUILTIN_ROOT, "public")]
        pub = home / "public"
        if pub.exists():
            # registry repos (e.g. RamenSOPs) keep their SOPs under sops/
            roots += [((p / "sops") if (p / "sops").is_dir() else p, "public") for p in sorted(pub.iterdir()) if p.is_dir()]
        roots += [(home / "sops", "private"), (cwd / ".rameness" / "sops", "private")]
        here = lambda d: (cwd / Path(str(d)).expanduser()).resolve()    # absolute paths stay as they are
        for d in private or []:
            if all(here(d) != r for r, _ in roots):
                roots.append((here(d), "private"))
        lib = cls(roots, cwd / ".rameness" / "sop_stats.json")
        lib.save_to = here(save_to) if save_to else None
        return lib

    def add_root(self, path: Path, scope: str = "public") -> None:
        """Add a package root below the private roots (so private SOPs still override it)."""
        if any(r == path for r, _ in self.roots):
            return
        first_private = next((i for i, (_, sc) in enumerate(self.roots) if sc == "private"), len(self.roots))
        self.roots.insert(first_private, (path, scope))
        self.reload()

    @property
    def private_root(self) -> Path:
        """Where new private SOPs are written."""
        return getattr(self, "save_to", None) or self.roots[-1][0]

    def reload(self) -> None:
        self.root = Node("", "all capabilities")
        self.sops: dict[str, SOP] = {}
        self.aliases: dict[str, str] = {}         # old id -> new id, for SOPs moved when a category split
        for base, scope in self.roots:
            if base.exists():
                self._scan(base, base, scope)
                f = base / "_aliases.json"
                if f.exists():
                    try:
                        self.aliases.update(json.loads(f.read_text()))
                    except json.JSONDecodeError:
                        pass

    def _node(self, node_id: str) -> Node:
        n = self.root
        parts = [p for p in node_id.split(".") if p]
        for i, p in enumerate(parts):
            n = n.children.setdefault(p, Node(".".join(parts[: i + 1])))
        return n

    def _scan(self, base: Path, d: Path, scope: str) -> None:
        rel = ".".join(d.relative_to(base).parts)
        if (d / "sop.json").exists():
            try:
                sop = SOP.load(d, rel, scope)
            except (json.JSONDecodeError, TypeError) as e:
                print(f"rameness: skipping bad SOP {d}: {e}", file=sys.stderr)
                return
            self._node(sop.id).sop = sop
            self.sops[sop.id] = sop
            return
        if rel:
            n = self._node(rel)
            meta = d / "_node.json"
            if meta.exists():
                m = json.loads(meta.read_text())
                n.description = m.get("description", n.description)
                n.keywords = sorted(set(n.keywords) | set(m.get("keywords", [])))
                reqs = {r.name: r for r in n.requires}
                for r in m.get("requires", []):
                    reqs[r["name"]] = Requirement(r["name"], r.get("hints", []), r.get("question", ""))
                n.requires = list(reqs.values())
        for c in sorted(d.iterdir()):
            if c.is_dir() and not c.name.startswith((".", "_")):
                self._scan(base, c, scope)

    def resolve(self, sop_id: str) -> str:
        """The SOP's current id: an id from before a category split still works."""
        seen = set()
        while sop_id not in self.sops and sop_id in getattr(self, "aliases", {}) and sop_id not in seen:
            seen.add(sop_id)
            sop_id = self.aliases[sop_id]
        return sop_id

    def get(self, sop_id: str) -> SOP:
        sop_id = self.resolve(sop_id)
        if sop_id not in self.sops:
            raise KeyError(f"unknown SOP {sop_id!r}")
        return self.sops[sop_id]

    def find_by_tool(self, tool_name: str) -> SOP | None:
        return next((s for s in self.sops.values() if s.tool_name == tool_name), None)

    # ---- stats (kept out of package dirs so public packages stay pristine)

    def ok_uses(self, sop_id: str) -> int:
        return int(self.stats.get(sop_id, {}).get("ok", 0))

    def success_rate(self, sop_id: str) -> float:
        s = self.stats.get(sop_id, {})
        return (s.get("ok", 0) + 1) / (s.get("uses", 0) + 2)

    def record_savings(self, sop_id: str, tokens: float, seconds: float) -> None:
        """Add one measured use's savings to the SOP's running totals (the evidence for sharing it)."""
        s = self.stats.setdefault(sop_id, {"uses": 0, "ok": 0})
        s["measured_uses"] = s.get("measured_uses", 0) + 1
        s["tokens_saved"] = round(s.get("tokens_saved", 0) + tokens)
        s["seconds_saved"] = round(s.get("seconds_saved", 0.0) + seconds, 2)
        if self.stats_path:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            self.stats_path.write_text(json.dumps(self.stats, indent=1))

    def record_use(self, sop_id: str, ok: bool) -> None:
        s = self.stats.setdefault(sop_id, {"uses": 0, "ok": 0})
        s["uses"] += 1
        s["ok"] += int(ok)
        s["last"] = time.time()
        if self.stats_path:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            self.stats_path.write_text(json.dumps(self.stats, indent=1))

    def tree(self, node: Node | None = None, indent: int = 0) -> str:
        node = node or self.root
        lines = []
        for name, c in sorted(node.children.items()):
            if c.sop:
                tag = f" [{c.sop.kind}, {c.sop.scope}{', candidate' if c.sop.status != 'validated' else ''}]"
                lines.append("  " * indent + f"- {name}{tag}: {c.sop.description}")
            else:
                req = f" (requires: {', '.join(r.name for r in c.requires)})" if c.requires else ""
                lines.append("  " * indent + f"+ {name}/{req} {c.description}")
                lines.append(self.tree(c, indent + 1))
        return "\n".join(l for l in lines if l)

    def search(self, jev: Jev, query: str, k: int = 8) -> list[tuple[SOP, float]]:
        """Flat search over leaves (used by the model-facing ``sop_search`` tool)."""
        sops = [s for s in self.sops.values() if s.status == "validated"]   # candidates stay hidden
        if not sops:
            return []
        d = jev.activate("Which procedures match this request?", query,
                         [Option(s.id, s.text, self._prior(s), s.desc) for s in sops])
        return [(self.sops[i], p) for i, p in d.top(k) if p > 0.1]

    def _prior(self, s: SOP) -> float:
        base = 0.6 + 0.8 * self.success_rate(s.id)     # 0.6 .. 1.4
        return base * (0.7 if s.status != "validated" else 1.0)


# --------------------------------------------------------------------------- activation

@dataclass
class Activation:
    selected: list[tuple[SOP, float]] = field(default_factory=list)
    deferred: list[tuple[Node, float, list[Requirement]]] = field(default_factory=list)
    explored: list[tuple[str, float, str]] = field(default_factory=list)   # (node, p, verdict)
    jev_calls: int = 0

    @property
    def missing(self) -> list[Requirement]:
        seen, out = set(), []
        for _, _, reqs in self.deferred:
            for r in reqs:
                if r.name not in seen:
                    seen.add(r.name)
                    out.append(r)
        return out

    def report(self) -> str:
        lines = [f"  {v:8s} {p:.2f}  {n}" for n, p, v in self.explored]
        return "\n".join(lines)


def unresolved(node: Node, text: str, defaults: dict) -> list[Requirement]:
    low = text.lower()
    out = []
    for r in node.requires:
        if r.name in defaults or any(h.lower() in low for h in r.hints):
            continue
        out.append(r)
    return out


def activate(lib: Library, jev: Jev, task: str, context: str = "", defaults: dict | None = None,
             activate_th: float = 0.55, explore_th: float = 0.3, beam: int = 4, max_sops: int = 8) -> Activation:
    """Top-down traversal of the SOP tree.

    At each category the JEV scores the children in one call. A child is
    rejected (< explore_th), explored (descend), selected (leaf >= activate_th),
    or deferred: relevant, but the information needed to pick among its
    children (``requires``) is missing, so the search stops there and resumes
    once the task state includes it.
    """
    defaults = defaults or {}
    act = Activation()
    frontier: list[tuple[Node, float]] = [(lib.root, 1.0)]
    full = f"{task}\n{context}"
    before = jev.calls
    while frontier:
        frontier.sort(key=lambda x: -x[1])
        node, _ = frontier.pop(0)
        kids = [k for k in node.children.values() if not (k.sop and k.sop.status != "validated")]
        if not kids:                               # untested candidates are never offered to the agent
            continue
        # JEV sees the task only; the org context resolves `requires` deterministically (unresolved)
        d = jev.activate(f"Which capabilities under '{node.id or 'root'}' will this task need?", task,
                         [Option(k.id, k.text, lib._prior(k.sop) if k.sop else 1.0, k.desc) for k in kids])
        ranked = sorted(zip(kids, (d.probs[k.id] for k in kids)), key=lambda x: -x[1])
        for rank, (k, p) in enumerate(ranked):
            if p < explore_th or rank >= beam:
                act.explored.append((k.id, p, "reject"))
                continue
            if k.sop:
                if p >= activate_th:
                    act.selected.append((k.sop, p))
                    act.explored.append((k.id, p, "select"))
                else:
                    act.explored.append((k.id, p, "weak"))
                continue
            missing = unresolved(k, full, defaults)
            if missing:
                act.deferred.append((k, p, missing))
                act.explored.append((k.id, p, "defer"))
            else:
                frontier.append((k, p))
                act.explored.append((k.id, p, "explore"))
    act.selected.sort(key=lambda x: -x[1])
    act.selected = act.selected[:max_sops]
    act.jev_calls = jev.calls - before
    return act


# --------------------------------------------------------------------------- execution

class SOPError(Exception):
    pass


def validate_args(sop: SOP, args: dict) -> list[str]:
    errs = []
    props = sop.inputs.get("properties", {})
    for r in sop.inputs.get("required", []):
        if r not in args:
            errs.append(f"missing required argument '{r}'")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
    for k, v in args.items():
        t = props.get(k, {}).get("type")
        if t in types and not isinstance(v, types[t]):
            errs.append(f"argument '{k}' should be {t}")
    return errs


_TPL = re.compile(r"\$\{([^}]+)\}")


def _lookup(scope: dict, path: str):
    cur = scope
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


def _render(v, scope):
    if isinstance(v, str):
        m = _TPL.fullmatch(v)
        if m:
            return _lookup(scope, m.group(1))
        return _TPL.sub(lambda mm: str(_lookup(scope, mm.group(1))), v)
    if isinstance(v, dict):
        return {k: _render(x, scope) for k, x in v.items()}
    if isinstance(v, list):
        return [_render(x, scope) for x in v]
    return v


class Executor:
    def __init__(self, lib: Library, allow: list[str], approve=None, cwd: Path | None = None, timeout: int = 120,
                 env=None):
        self.env = env if env is not None and getattr(env, "kind", "local") != "local" else None
        self._shipped: set[str] = set()
        self.lib = lib
        self.allow = set(allow)
        self.approve = approve or (lambda sop, args: False)
        self.cwd = cwd or Path.cwd()
        self.timeout = timeout

    def permitted(self, sop: SOP, args: dict) -> bool:
        extra = [p for p in sop.permissions if p not in self.allow]
        return not extra or self.approve(sop, args)

    def run(self, sop_id: str, args: dict, _depth: int = 0, preapproved: bool = False) -> dict:
        """``preapproved``: the user already chose to run this SOP (a configured lifecycle hook)."""
        sop = self.lib.get(sop_id)
        errs = validate_args(sop, args)
        if errs:
            raise SOPError(f"{sop_id}: " + "; ".join(errs))
        if _depth == 0 and not preapproved and not self.permitted(sop, args):
            raise SOPError(f"{sop_id}: permission denied ({', '.join(sop.permissions)})")
        ok = False
        try:
            if sop.kind == "script":
                result = self._script(sop, args)
            elif sop.kind == "composite":
                result = self._composite(sop, args, _depth)
            elif sop.kind == "skill":
                result = {"instructions": sop.instructions}
            else:
                raise SOPError(f"unknown SOP kind {sop.kind}")
            ok = True
            return result
        finally:
            if _depth == 0:
                self.lib.record_use(sop_id, ok)

    def _script(self, sop: SOP, args: dict) -> dict:
        if self.env is not None:
            return self._script_remote(sop, args)
        entry = sop.path / sop.entry
        cmd = {".py": [sys.executable, str(entry)], ".sh": ["bash", str(entry)]}.get(entry.suffix, [str(entry)])
        if sop.scope == "public":
            # Public code must not inherit provider keys, cloud credentials or Python import overrides.
            public_env = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR"}
            env = {k: v for k, v in os.environ.items() if k in public_env}
            if entry.suffix == ".py":
                cmd = [sys.executable, "-I", "-B", str(entry)]
        else:
            env = dict(os.environ)
        env["RAMENESS_SOP_DIR"] = str(sop.path)
        p = subprocess.run(cmd, input=json.dumps(args), capture_output=True, text=True, cwd=self.cwd,
                           timeout=self.timeout, env=env)
        if p.returncode != 0:
            raise SOPError(f"{sop.id} exited {p.returncode}: {(p.stderr or p.stdout)[-2000:]}")
        out = p.stdout.strip()
        try:
            result = json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"output": out}
        if not isinstance(result, dict):
            raise SOPError(f"{sop.id}: script must output a JSON object, got {type(result).__name__}")
        return result

    def _script_remote(self, sop: SOP, args: dict) -> dict:
        """Ship the SOP directory to the environment once, then run it there."""
        if not getattr(self, "_remote_root", None):
            # a private staging dir (mode 700, unique name): a fixed shared path breaks for a second user and
            # would let another user plant scripts there
            r = self.env.run("mktemp -d \"${TMPDIR:-/tmp}/rameness-sops.XXXXXXXX\"", str(self.cwd), timeout=30)
            if r.code != 0 or not r.out.strip():
                raise SOPError(f"could not create a staging directory on {self.env.id}: {(r.err or r.out)[-500:]}")
            self._remote_root = r.out.strip().splitlines()[-1]
        remote = f"{self._remote_root}/{sop.id}-{sop.version}"
        if sop.id not in self._shipped:
            self.env.put_dir(sop.path, remote)
            self._shipped.add(sop.id)
        runner = {".py": "python3", ".sh": "bash"}.get(Path(sop.entry).suffix, "")
        command = f"RAMENESS_SOP_DIR={remote} {runner} {remote}/{sop.entry}"
        if sop.scope == "public":
            argv = ([runner] if runner else []) + (["-I", "-B"] if Path(sop.entry).suffix == ".py" else []) + [f"{remote}/{sop.entry}"]
            command = ('env -i PATH="$PATH" LANG="${LANG:-C.UTF-8}" '
                       + "RAMENESS_SOP_DIR=" + shlex.quote(remote) + " " + shlex.join(argv))
        r = self.env.run(command, str(self.cwd), timeout=self.timeout, input=json.dumps(args))
        if r.code != 0:
            raise SOPError(f"{sop.id} exited {r.code} on {self.env.id}: {(r.err or r.out)[-2000:]}")
        out = r.out.strip()
        try:
            result = json.loads(out) if out else {}
        except json.JSONDecodeError:
            return {"output": out}
        if not isinstance(result, dict):
            raise SOPError(f"{sop.id}: script must output a JSON object, got {type(result).__name__}")
        return result

    def _composite(self, sop: SOP, args: dict, depth: int) -> dict:
        if depth > 8:
            raise SOPError("composite SOP nesting too deep")
        scope = {"input": args, "steps": []}
        for step in sop.steps:
            sub = self.lib.get(step["sop"])
            if not self.permitted(sub, args):
                raise SOPError(f"{sub.id}: permission denied")
            res = self.run(sub.id, _render(step.get("args", {}), scope), depth + 1)
            scope["steps"].append(res)
        ret = sop.outputs.get("return")
        return _render(ret, scope) if ret else scope["steps"][-1] if scope["steps"] else {}

    def _run_test(self, sop_id: str, t: dict) -> dict:
        """One test, always in a fresh folder: never the project's own directory, whose files a test that writes
        (a report, an output path) would overwrite. Fixtures (``files``: relative path -> text; ``setup``: Python
        that builds anything else, e.g. binary files) are created there, and inputs refer to them by relative path."""
        if self.env is not None:
            if t.get("files") or t.get("setup"):
                raise SOPError("test fixtures need a local environment")
            r = self.env.run("mktemp -d \"${TMPDIR:-/tmp}/rameness-sop-test.XXXXXXXX\"", str(self.cwd), timeout=30)
            if r.code != 0 or not r.out.strip():
                raise SOPError(f"could not create a test directory on {self.env.id}: {(r.err or r.out)[-500:]}")
            prev, self.cwd = self.cwd, Path(r.out.strip().splitlines()[-1])
            try:
                return self.run(sop_id, t.get("input", {}), _depth=1)
            finally:
                self.cwd = prev
                self.env.run(f"rm -rf {shlex.quote(r.out.strip().splitlines()[-1])}", str(prev), timeout=30)
        prev = self.cwd
        with tempfile.TemporaryDirectory(prefix="rameness-sop-test-") as tmp:
            root = Path(tmp)
            for rel, text in (t.get("files") or {}).items():
                target = (root / rel).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise SOPError(f"fixture path {rel!r} leaves the test folder")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(text))
            if t.get("setup"):
                r = subprocess.run([sys.executable, "-c", str(t["setup"])], cwd=root, capture_output=True, text=True,
                                   timeout=60)
                if r.returncode:
                    raise SOPError(f"test setup failed: {(r.stderr or r.stdout).strip()[-400:]}")
            self.cwd = root
            try:
                return self.run(sop_id, t.get("input", {}), _depth=1)
            finally:
                self.cwd = prev

    def test(self, sop_id: str) -> list[str]:
        """Run the SOP's embedded tests. Returns failure messages (empty = pass)."""
        sop = self.lib.get(sop_id)
        failures = []
        self.last_outputs: list = []               # what each test actually returned (for repairing the SOP)
        for i, t in enumerate(sop.tests):
            try:
                res = self._run_test(sop_id, t)
            except Exception as e:
                self.last_outputs.append({"error": str(e)[:600]})
                if not t.get("expect_error"):
                    failures.append(f"test {i}: raised {e}")
                continue
            self.last_outputs.append(res)
            if t.get("expect_error"):
                failures.append(f"test {i}: expected an error")
            for k, v in t.get("expect", {}).items():
                try:
                    got = _lookup(res, k)
                except (KeyError, IndexError, TypeError):
                    failures.append(f"test {i}: missing {k}")
                    continue
                if got != v:
                    failures.append(f"test {i}: {k} = {got!r}, expected {v!r}")
            for k in t.get("expect_keys", []):
                if k not in res:
                    failures.append(f"test {i}: missing key {k}")
            if not (t.get("expect") or t.get("expect_keys") or t.get("expect_error")):
                failures.append(f"test {i}: asserts nothing (no expect, expect_keys or expect_error)")
        return failures
