"""Procedural learning: turn repeated work into SOPs.

After each task the trace (tool calls) is stored. The learner then:

1. mines step n-grams that recur across *different* runs (frequency signal),
2. optionally asks the fast LLM to segment the trace into generalised steps,
3. asks the JEV which steps look like standard procedures (judgement signal),
4. combines both with a noisy-OR:  p = 1 - (1 - p_jev) * (1 - p_freq),
5. drops candidates that duplicate an existing SOP (JEV match against library),
6. generates a script + tests (LLM), or for exact repeated shell sequences
   emits a deterministic script without any LLM,
7. registers it in the private library - ``validated`` only if its tests pass,
   otherwise ``candidate`` (callable by the model, never auto-executed).
"""

from __future__ import annotations

import json
import math
import os
import py_compile
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .jev import Jev, Option
from .sops import SOP, Executor, Library


def shape(step: dict) -> str:
    """Parameter-insensitive signature of a step."""
    if step["tool"] == "bash":
        cmd = step["input"].get("command", "")
        cmd = re.sub(r"(['\"]).*?\1", "STR", cmd)
        cmd = re.sub(r"\b\d+(\.\d+)?\b", "N", cmd)
        cmd = re.sub(r"(?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+", "PATH", cmd)
        return "bash:" + " ".join(cmd.split()[:4])
    if step["tool"].startswith("sop_"):
        return step["tool"]
    return step["tool"] + ":" + ",".join(sorted(step["input"]))


def exact(step: dict) -> str:
    return step["tool"] + ":" + json.dumps(step["input"], sort_keys=True)


@dataclass
class Candidate:
    name: str
    description: str
    steps: list[dict]
    count: int = 1
    exact_repeat: bool = False
    p_jev: float = 0.0
    score: float = 0.0
    params: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        cmds = "; ".join(s["input"].get("command", s["tool"]) for s in self.steps)
        return f"{self.name}: {self.description} ({cmds[:300]})"


class RunStore:
    def __init__(self, root: Path):
        self.root = root

    def save(self, task: str, steps: list[dict], success: bool, extra: dict | None = None) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        (self.root / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "task": task, "steps": steps, "success": success, **(extra or {})}, default=str))
        return rid

    def all(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except json.JSONDecodeError:
                continue
        return out


def mine_repeats(runs: list[dict], min_repeats: int = 2, max_n: int = 4) -> list[Candidate]:
    """Step n-grams whose shape recurs in >= min_repeats distinct successful runs."""
    seen: dict[tuple, dict] = {}
    for r in runs:
        if not r.get("success"):
            continue
        steps = [s for s in r["steps"] if s.get("ok", True) and not s["tool"].startswith(("sop_", "recall", "read_file", "grep"))]
        local = set()
        for n in range(1, max_n + 1):
            for i in range(len(steps) - n + 1):
                seg = steps[i:i + n]
                key = tuple(shape(s) for s in seg)
                if key in local:
                    continue
                local.add(key)
                e = seen.setdefault(key, {"runs": 0, "example": seg, "exacts": set(), "tasks": []})
                e["runs"] += 1
                e["exacts"].add(tuple(exact(s) for s in seg))
                e["tasks"].append(r["task"][:120])
    cands = []
    for key, e in seen.items():
        if e["runs"] < min_repeats:
            continue
        if all(k.startswith(("write_file", "edit_file", "edit_lines")) for k in key):
            continue      # raw edits are content, not procedure
        cands.append(Candidate(name=" -> ".join(key), description=f"seen in {e['runs']} tasks: " + " | ".join(e["tasks"][:3]),
                               steps=e["example"], count=e["runs"], exact_repeat=len(e["exacts"]) == 1))
    # prefer maximal sequences: drop a candidate contained in a longer one with the same count
    cands.sort(key=lambda c: (-len(c.steps), -c.count))
    kept: list[Candidate] = []
    for c in cands:
        if not any(c.name in k.name and c.count <= k.count for k in kept):
            kept.append(c)
    return kept


SEGMENT_SYSTEM = "You analyse agent execution traces and extract reusable procedures. Output JSON only."

GENERATE_SYSTEM = """You write small Python SOP scripts for an agent harness. Use only the Python standard library and
programs every Linux system has (sh, grep, sed, awk, find, sort, git...); use an extra package or program only when
nothing standard can do the job, and never one that duplicates the standard library.
Contract: the script reads a JSON object of arguments from stdin and prints ONE JSON object to stdout.
Exit non-zero with a message on stderr on failure. Parameterise everything that varied or could vary
(paths, URLs, names); never hard-code secrets, credentials, hostnames, or organisation-specific values.
Output JSON only."""


SIMPLIFY_SYSTEM = """You simplify SOP scripts (Occam's razor). Rewrite the script so it needs nothing beyond the Python
standard library and programs every Linux system has (sh, grep, sed, awk, find, sort, git...). For example: urllib.request
instead of requests, json instead of jq, csv instead of pandas for simple tables, subprocess with base tools instead of
extra binaries. Keep the exact same arguments, outputs and behaviour. If an extra package or program is truly
unavoidable (no standard way exists), reply {"keep": true, "why": "..."}. Otherwise reply {"script": "<python source>"}.
Output JSON only."""


def simplify_sop(lib: Library, ex: Executor, sop_id: str, llm) -> dict:
    """Occam's razor for an SOP that needs extra packages or programs: ask for a standard-only rewrite and keep
    it only if it passes the SOP's own tests and needs less. The tests decide, not the model's claim."""
    from .deps import requirements
    sop = lib.get(sop_id)
    before = {k: v for k, v in (sop.requirements or {}).items() if v}
    if not before or llm is None or sop.kind != "script" or not sop.tests:
        return {"simplified": False, "reason": "nothing to simplify" if not before else "no model or tests"}
    src = (sop.path / sop.entry).read_text()
    try:
        r = llm.complete_json(SIMPLIFY_SYSTEM, (
            f"SOP {sop.id}: {sop.description}\nIt needs: {json.dumps(before)}\nInputs: {json.dumps(sop.inputs)}\n"
            f"Outputs: {json.dumps(sop.outputs)}\nTests: {json.dumps(sop.tests)[:3000]}\n"
            f"Current {sop.entry}:\n{src[:12000]}"), max_tokens=8000)
    except Exception as e:
        return {"simplified": False, "reason": f"rewrite failed: {e}"}
    if not isinstance(r, dict) or r.get("keep") or not isinstance(r.get("script"), str):
        return {"simplified": False, "reason": "kept: " + str((r or {}).get("why", "no rewrite offered"))[:200]}
    tmp = _write_temp(r["script"])
    try:
        py_compile.compile(tmp, doraise=True)
    except Exception as e:
        return {"simplified": False, "reason": f"rewrite does not compile: {e}"}
    finally:
        os.unlink(tmp)
    old_files = {f.name: f.read_text() for f in sop.path.iterdir() if f.name in ("run.py", "run.sh")}
    old_entry = sop.entry
    for f in old_files:
        (sop.path / f).unlink()
    (sop.path / "run.py").write_text(r["script"])
    after = {k: v for k, v in requirements(sop.path).items() if v}
    data = json.loads((sop.path / "sop.json").read_text())
    data["entry"] = "run.py"
    (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
    lib.reload()
    failures = ex.test(sop_id)
    fewer = sum(map(len, after.values())) < sum(map(len, before.values()))
    if failures or not fewer:
        (sop.path / "run.py").unlink()
        for name, text in old_files.items():
            (sop.path / name).write_text(text)
        data["entry"] = old_entry
        (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
        lib.reload()
        return {"simplified": False, "reason": "rewrite failed its tests" if failures else "rewrite needed no less"}
    data["requirements"] = after
    data.setdefault("origin", {})["simplified"] = {"removed": {k: sorted(set(v) - set(after.get(k, [])))
                                                              for k, v in before.items()}}
    if not after:
        data.pop("requirements", None)
    (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
    lib.reload()
    return {"simplified": True, "before": before, "after": after}


def _write_temp(source: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".py", prefix="rameness-simplify-")
    with os.fdopen(fd, "w") as f:
        f.write(source)
    return path


EXTEND_SYSTEM = """You extend an existing SOP script so it also covers a new, similar procedure. Keep everything it
already does working exactly as before: keep every existing input and output; any new input must be optional with a
default that reproduces the old behaviour. Change as little as possible, and use only the Python standard library and
programs every Linux system has unless the existing script already needs more. Reply {"script": "<python source>",
"inputs": <full JSON schema>, "outputs": {...}, "description": str, "keywords": [..],
"tests": [<new tests for the added behaviour, each {"input": {...}, "expect": {...}}>]}. Output JSON only."""


def _bump(version: str) -> str:
    parts = (version or "1.0.0").split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    except ValueError:
        return "1.1.0"


def extend_sop(lib: Library, ex: Executor, sop: SOP, spec: dict, llm, origin: dict | None = None) -> SOP | None:
    """Extend ``sop`` to also do what ``spec`` describes, instead of creating a near-duplicate.

    Accepted only when every existing test still passes, the new tests pass, no existing input is dropped and
    no new input is required: then a private SOP is updated in place, and a built-in or registry SOP gets a
    private override with the same id (later library layers win, and proposing it updates the original).
    Returns the extended SOP, or None (nothing is changed) so the caller creates a new SOP instead."""
    if llm is None or sop.kind != "script" or not sop.tests:
        return None
    entry = sop.path / sop.entry
    try:
        r = llm.complete_json(EXTEND_SYSTEM, (
            f"Existing SOP {sop.id}: {sop.description}\nInputs: {json.dumps(sop.inputs)}\n"
            f"Outputs: {json.dumps(sop.outputs)}\nExisting tests: {json.dumps(sop.tests)[:3000]}\n"
            f"Existing {sop.entry}:\n{entry.read_text()[:12000]}\n\n"
            f"New procedure to cover as well: {spec.get('id', '')}: {spec.get('description', '')}\n"
            f"Its inputs: {json.dumps(spec.get('inputs') or {})}\nIts tests: {json.dumps(spec.get('tests') or [])[:3000]}\n"
            f"Its code:\n{(spec.get('script') or spec.get('shell') or '')[:8000]}"), max_tokens=10000)
    except Exception:
        return None
    if not isinstance(r, dict) or not isinstance(r.get("script"), str):
        return None
    old_props = set((sop.inputs or {}).get("properties", {}))
    new_inputs = r.get("inputs") or sop.inputs
    new_props = set((new_inputs or {}).get("properties", {}))
    old_req = set((sop.inputs or {}).get("required", []))
    if not old_props <= new_props or not set((new_inputs or {}).get("required", [])) <= old_req:
        return None                                     # would break existing callers
    new_tests = [t for t in (r.get("tests") or []) if isinstance(t, dict) and "input" in t]
    if not new_tests:
        return None
    # Build the candidate in a scratch library layer, so a rejected extension leaves nothing behind.
    scratch = Path(tempfile.mkdtemp(prefix="rameness-extend-"))
    try:
        cand_dir = scratch.joinpath(*sop.id.split("."))
        shutil.copytree(sop.path, cand_dir, ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("run.py", "run.sh"):
            (cand_dir / f).unlink(missing_ok=True)
        (cand_dir / "run.py").write_text(r["script"])
        data = json.loads((cand_dir / "sop.json").read_text())
        data.update(entry="run.py", inputs=new_inputs, outputs=r.get("outputs") or sop.outputs,
                    description=r.get("description") or sop.description,
                    keywords=sorted(set(sop.keywords) | set(r.get("keywords") or [])),
                    tests=list(sop.tests) + new_tests, version=_bump(sop.version), status="candidate")
        (cand_dir / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
        trial = Library([(scratch, sop.scope)])
        if Executor(trial, list(ex.allow), approve=ex.approve, cwd=ex.cwd, env=ex.env).test(sop.id):
            return None                                 # an old or a new test fails: keep the SOP as it was
        from .deps import requirements
        data["requirements"] = {k: v for k, v in requirements(cand_dir).items() if v}
        data["status"] = "validated"
        ext = {"t": time.time(), "adds": spec.get("description", "")[:200], "from": sop.version or "1.0.0",
               **(origin or {})}
        data.setdefault("origin", {}).setdefault("extended", []).append(ext)
        if sop.scope != "private":                     # a private copy of a built-in or registry SOP
            from .sops import BUILTIN_ROOT
            data["origin"]["overrides"] = "builtin" if sop.path.is_relative_to(BUILTIN_ROOT) else "registry"
        (cand_dir / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
        target = lib.private_root.joinpath(*sop.id.split(".")) if sop.scope != "private" else sop.path
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cand_dir, target)
        for i in range(1, len(sop.id.split("."))):     # keep category metadata alongside an override
            node = sop.path.parents[i - 1] / "_node.json"
            dst = target.parents[i - 1] / "_node.json"
            if node.exists() and not dst.exists():
                shutil.copy(node, dst)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    lib.reload()
    return lib.get(sop.id)


def register_sop(lib: Library, ex: Executor, spec: dict, origin: dict | None = None,
                 root: Path | None = None, jev: Jev | None = None, org=None, llm=None) -> tuple[SOP, list[str]]:
    """Write an SOP to the private library, compile + test it, set its status."""
    sop_id = re.sub(r"[^a-z0-9_.]", "_", spec["id"].lower()).strip("._")
    if "." not in sop_id:
        sop_id = "learned." + sop_id
    root = root or lib.private_root
    path = root.joinpath(*sop_id.split("."))
    if path.exists() and (path / "sop.json").exists():
        sop_id += "_" + uuid.uuid4().hex[:4]
        path = root.joinpath(*sop_id.split("."))
    tmp = Path(tempfile.mkdtemp(prefix="rameness-sop-"))
    try:
        entry = "run.sh" if spec.get("shell") else "run.py"
        (tmp / entry).write_text(spec.get("shell") or spec["script"])
        if entry == "run.py":
            py_compile.compile(str(tmp / entry), doraise=True)
        sop = SOP(id=sop_id, path=path, description=spec["description"], kind="script", entry=entry,
                  inputs=spec.get("inputs") or {"type": "object", "properties": {}},
                  outputs=spec.get("outputs", {}), permissions=spec.get("permissions", ["exec"]),
                  keywords=spec.get("keywords", []), tests=spec.get("tests", []), status="candidate",
                  scope="private", origin={"created": time.time(), **(origin or {})})
        path.mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp / entry, path / entry)
        from .deps import requirements
        sop.requirements = {k: v for k, v in requirements(path).items() if v}   # what it may need installed
        sop.save()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lib.reload()
    if jev is not None:
        # JEV files it under the category it belongs in (the generator only proposes one)
        from .publish import categorize, recategorize
        sop_id = recategorize(lib, lib.get(sop_id), categorize(jev, lib, lib.get(sop_id))).id
    if llm is not None and sop.tests:
        simplify_sop(lib, ex, sop_id, llm)       # Occam's razor: nothing extra if the standard tools can do it
    failures = ex.test(sop_id) if sop.tests else ["no tests provided"]
    sop = lib.get(sop_id)
    sop.status = "validated" if not failures else "candidate"
    sop.save()
    lib.reload()
    if jev is not None:
        # JEV decides personal vs general right away; anything uncertain stays private
        from .org import Org
        from .publish import classify
        classify(jev, lib.get(sop_id), org or Org())
        lib.reload()
    return lib.get(sop_id), failures


class Learner:
    def __init__(self, lib: Library, ex: Executor, jev: Jev, runs: RunStore, llm=None,
                 min_repeats: int = 2, threshold: float = 0.6, auto_generate: bool = True, org=None):
        self.lib, self.ex, self.jev, self.runs, self.llm = lib, ex, jev, runs, llm
        self.org = org
        self.min_repeats = min_repeats
        self.threshold = threshold
        self.auto_generate = auto_generate

    # ---- candidate discovery

    def _llm_segments(self, task: str, steps: list[dict]) -> list[Candidate]:
        if not self.llm or len(steps) < 2:
            return []
        trace = "\n".join(f"{i}. {s['tool']} {json.dumps(s['input'])[:300]} -> {'ok' if s.get('ok', True) else 'error'}"
                          for i, s in enumerate(steps))
        try:
            data = self.llm.complete_json(SEGMENT_SYSTEM, (
                f"Task: {task}\nTrace:\n{trace}\n\n"
                "Group the successful steps into generic sub-procedures (ignore dead ends). For each give "
                '{"name": snake_case, "description": generic one-liner, "steps": [indices], '
                '"params": [names of values that would change next time], "p_reusable": 0..1}. '
                'Reply {"procedures": [...]}'))
        except Exception:
            return []
        out = []
        for p in data.get("procedures", []):
            idx = [i for i in p.get("steps", []) if isinstance(i, int) and 0 <= i < len(steps)]
            if idx:
                out.append(Candidate(p.get("name", "procedure"), p.get("description", ""), [steps[i] for i in idx],
                                     params=p.get("params", []), p_jev=float(p.get("p_reusable", 0))))
        return out

    def candidates(self, task: str, steps: list[dict]) -> list[Candidate]:
        cands = [c for c in mine_repeats(self.runs.all(), self.min_repeats)
                 if any(shape(s) in {shape(x) for x in steps} for s in c.steps)]   # related to this run
        cands += self._llm_segments(task, steps)
        if not cands:
            return []
        d = self.jev.activate("Which of these steps is a standard, reusable procedure likely to recur in future tasks?",
                              task, [Option(str(i), c.text) for i, c in enumerate(cands)])
        for i, c in enumerate(cands):
            p_freq = 1 - math.exp(-(c.count - 1)) if c.count > 1 else 0.0
            p_jev = max(c.p_jev, d.probs[str(i)])
            c.score = 1 - (1 - p_jev) * (1 - p_freq)
        return sorted(cands, key=lambda c: -c.score)

    def _duplicate(self, c: Candidate) -> str | None:
        existing = list(self.lib.sops.values())
        if not existing:
            return None
        d = self.jev.activate("Does an existing SOP already perform this procedure?", c.text,
                              [Option(s.id, s.text, desc=s.desc) for s in existing])
        sid, p = d.top(1)[0]
        return sid if p >= 0.8 else None

    def _duplicate_generated(self, spec: dict) -> str | None:
        """Compare the generated behavior too: a trace's name can hide an existing procedure."""
        existing = [s for s in self.lib.sops.values() if s.status == "validated" and s.kind == "script"]
        if not existing:
            return None
        query = f"{spec.get('id', '')}: {spec.get('description', '')}"
        d = self.jev.activate("Which existing SOPs might already cover this generated procedure?", query,
                              [Option(s.id, s.text, desc=s.desc) for s in existing])
        by_id = {s.id: s for s in existing}
        for sid, probability in d.top(3):
            if probability < 0.3:
                continue
            sop = by_id[sid]
            state = (f"Proposed procedure: {query}\nInputs: {json.dumps(spec.get('inputs') or {})}\n"
                     f"Code:\n{spec.get('script') or spec.get('shell') or ''}\n\n"
                     f"Existing procedure: {sop.digest()}\nInputs: {json.dumps(sop.inputs)}")
            p = self.jev.yes("Can the existing SOP perform the proposed procedure using its current parameters, "
                             "without changing its code?", state,
                             "same equivalent already covered implements required behavior all outputs parameters",
                             "different partial missing behavior needs changes incompatible inputs outputs business rules",
                             yes_desc="Yes: the existing SOP already provides the proposed behavior.",
                             no_desc="No: it only partly overlaps or needs code changes to provide the behavior.")
            if p >= 0.8:
                return sid
        return None

    def extension_target(self, spec: dict) -> SOP | None:
        """An existing SOP that a small, backward-compatible change would make cover ``spec`` (JEV decides)."""
        existing = [s for s in self.lib.sops.values() if s.status == "validated" and s.kind == "script" and s.tests]
        if not existing:
            return None
        query = f"{spec.get('id', '')}: {spec.get('description', '')}"
        d = self.jev.activate("Which existing SOPs are close to this new procedure?", query,
                              [Option(s.id, s.text, desc=s.desc) for s in existing])
        by_id = {s.id: s for s in existing}
        for sid, probability in d.top(3):
            if probability < 0.3:
                continue
            sop = by_id[sid]
            state = (f"New procedure: {query}\nInputs: {json.dumps(spec.get('inputs') or {})}\n"
                     f"Code:\n{(spec.get('script') or spec.get('shell') or '')[:3000]}\n\n"
                     f"Existing procedure: {sop.digest()}\nInputs: {json.dumps(sop.inputs)}")
            p = self.jev.yes("Could the existing SOP cover the new procedure too with a small change, such as an "
                             "extra optional parameter or output, while still doing everything it does now?", state,
                             "same task similar extend extra option parameter flag variant small change also",
                             "different purpose unrelated separate rewrite incompatible",
                             yes_desc="Yes: a small, backward-compatible extension of the existing SOP covers it.",
                             no_desc="No: it is a different procedure, or would need a rewrite.")
            if p >= 0.7:
                return sop
        return None

    def extend_or_register(self, spec: dict, origin: dict | None = None) -> tuple[SOP, list[str], bool]:
        """Extend a close existing SOP when that works; otherwise register ``spec`` as a new SOP.
        Returns (sop, test failures, extended)."""
        target = self.extension_target(spec)
        if target is not None:
            extended = extend_sop(self.lib, self.ex, target, spec, self.llm, origin)
            if extended is not None:
                return extended, [], True
        sop, failures = register_sop(self.lib, self.ex, spec, origin, jev=self.jev, org=self.org, llm=self.llm)
        return sop, failures, False

    # ---- generation

    def _deterministic_spec(self, c: Candidate) -> dict | None:
        """Exact repeated shell sequences need no LLM: the script *is* the trace."""
        if not (c.exact_repeat and all(s["tool"] == "bash" for s in c.steps)):
            return None
        cmds = [s["input"]["command"] for s in c.steps]
        slug = re.sub(r"[^a-z0-9]+", "_", " ".join(cmds[0].split()[:3]).lower()).strip("_") or "procedure"
        body = "#!/usr/bin/env bash\nset -euo pipefail\n" + "\n".join(cmds) + "\n"
        return {"id": f"learned.{slug}", "description": f"Repeated procedure: {' && '.join(cmds)[:200]}",
                "shell": body, "keywords": slug.split("_"), "permissions": ["exec"], "tests": []}

    def _llm_spec(self, task: str, c: Candidate) -> dict | None:
        if not self.llm:
            return None
        cats = sorted(self.lib.root.children)
        try:
            return self.llm.complete_json(GENERATE_SYSTEM, (
                f"Original task: {task}\nProcedure: {c.name} - {c.description}\n"
                f"Parameters that vary: {c.params}\n"
                f"Concrete steps the agent executed:\n"
                + "\n".join(json.dumps({"tool": s["tool"], "input": s["input"]})[:800] for s in c.steps)
                + f"\n\nExisting top-level categories: {cats}\n"
                'Reply {"id": "category.name", "description": str, "keywords": [..], '
                '"inputs": <JSON schema>, "outputs": {...}, "permissions": subset of '
                '["fs:read","fs:write","network","exec","side-effect"], "script": <python source>, '
                '"tests": [{"input": {...}, "expect": {"output_field": expected_value}}]}. '
                'Tests must assert concrete output values for normal and edge cases, not just output keys. '
                'Error cases may use "expect_error": true. Tests must pass offline in a temp dir.'),
                max_tokens=8000)
        except Exception:
            return None

    def observe(self, task: str, steps: list[dict], success: bool, extra: dict | None = None) -> list[dict]:
        self.runs.save(task, steps, success, extra)
        if not success or not steps:
            return []
        created = []
        runs_total = sum(1 for r in self.runs.all() if r.get("success"))   # usage evidence for builtin vs registry
        for c in self.candidates(task, steps):
            if c.score < self.threshold:
                break
            dup = self._duplicate(c)
            if dup:
                created.append({"candidate": c.name, "skipped": f"duplicates {dup}"})
                continue
            spec = self._deterministic_spec(c) or (self._llm_spec(task, c) if self.auto_generate else None)
            if not spec:
                created.append({"candidate": c.name, "score": round(c.score, 2), "skipped": "no generator available"})
                continue
            dup = self._duplicate_generated(spec)
            if dup:
                created.append({"candidate": c.name, "skipped": f"generated procedure duplicates {dup}"})
                continue
            try:
                sop, failures, extended = self.extend_or_register(
                    spec, {"task": task[:200], "score": c.score, "runs_seen": c.count, "runs_total": runs_total})
            except Exception as e:
                created.append({"candidate": c.name, "error": str(e)})
                continue
            created.append({"sop": sop.id, "status": sop.status, "visibility": sop.visibility,
                            "score": round(c.score, 2), "failures": failures,
                            **({"extended": sop.version} if extended else {})})
            if len(created) >= 3:
                break
        return created
