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
        if all(k.startswith(("write_file", "edit_file")) for k in key):
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

GENERATE_SYSTEM = """You write small, dependency-free Python SOP scripts for an agent harness.
Contract: the script reads a JSON object of arguments from stdin and prints ONE JSON object to stdout.
Exit non-zero with a message on stderr on failure. Parameterise everything that varied or could vary
(paths, URLs, names); never hard-code secrets, credentials, hostnames, or organisation-specific values.
Output JSON only."""


def register_sop(lib: Library, ex: Executor, spec: dict, origin: dict | None = None,
                 root: Path | None = None, jev: Jev | None = None, org=None) -> tuple[SOP, list[str]]:
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
        sop.save()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lib.reload()
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
                              [Option(s.id, s.text) for s in existing])
        sid, p = d.top(1)[0]
        return sid if p >= 0.8 else None

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
                '"tests": [{"input": {...}, "expect_keys": [...]}]}. Tests must pass offline in a temp dir.'),
                max_tokens=8000)
        except Exception:
            return None

    def observe(self, task: str, steps: list[dict], success: bool, extra: dict | None = None) -> list[dict]:
        self.runs.save(task, steps, success, extra)
        if not success or not steps:
            return []
        created = []
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
            try:
                sop, failures = register_sop(self.lib, self.ex, spec, {"task": task[:200], "score": c.score},
                                             jev=self.jev, org=self.org)
            except Exception as e:
                created.append({"candidate": c.name, "error": str(e)})
                continue
            created.append({"sop": sop.id, "status": sop.status, "visibility": sop.visibility,
                            "score": round(c.score, 2), "failures": failures})
            if len(created) >= 3:
                break
        return created
