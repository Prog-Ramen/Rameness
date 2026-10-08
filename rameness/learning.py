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
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import schemas
from .jev import Jev, Option
from .sops import SOP, Executor, Library, keep_out_of_git


SUBCOMMAND_TOOLS = {"git", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "go", "docker", "kubectl",
                    "systemctl", "apt", "apt-get", "brew", "gh", "make", "poetry", "conda", "rustup", "terraform"}


def shape(step: dict) -> str:
    """Parameter-insensitive signature of a step."""
    if step["tool"] == "bash":
        cmd = step["input"].get("command", "")
        cmd = re.sub(r"(['\"]).*?\1", "STR", cmd)
        cmd = re.sub(r"\b\d+(\.\d+)?\b", "N", cmd)
        cmd = re.sub(r"(?:\.{0,2}/)?[\w.-]+(?:/[\w.-]+)+", "PATH", cmd)
        words = cmd.split()[:4]
        if not words:
            return "bash:"
        # bare arguments (`ls docs`, `pytest tests`) are parameters too; flags, operators and the subcommand of
        # tools like git or npm (`git status` vs `git commit`) are what make it a different procedure
        out, command, sub = [], True, False
        for w in words:
            if command:                                  # a command name: the first word, or after | && ; ||
                out.append(w)
                command, sub = False, w in SUBCOMMAND_TOOLS
            elif w in ("|", "||", "&&", ";"):
                out.append(w)
                command = True
            elif sub:                                    # git status / npm run: the subcommand stays
                out.append(w)
                sub = False
            else:
                out.append(w if w.startswith("-") or w in ("PATH", "STR", "N") or not re.match(r"^[\w.@:+-]+$", w)
                           else "ARG")
        return "bash:" + " ".join(out)
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
    projects: set = field(default_factory=set)     # the project folders whose runs repeated it
    key: tuple = ()                                # its step shapes (identifies it across checks)
    occurrences: list = field(default_factory=list)  # each repetition's steps (within one run)
    saves: dict = field(default_factory=dict)      # what one use would save: {"tokens", "seconds", "measured"}

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
                e = seen.setdefault(key, {"runs": 0, "example": seg, "exacts": set(), "tasks": [], "projects": set()})
                e["runs"] += 1
                e["exacts"].add(tuple(exact(s) for s in seg))
                e["tasks"].append(r["task"][:120])
                if r.get("project"):
                    e["projects"].add(r["project"])
    cands = []
    for key, e in seen.items():
        if e["runs"] < min_repeats:
            continue
        if all(k.startswith(("write_file", "edit_file", "edit_lines")) for k in key):
            continue      # raw edits are content, not procedure
        cands.append(Candidate(name=" -> ".join(key), description=f"seen in {e['runs']} tasks: " + " | ".join(e["tasks"][:3]),
                               steps=e["example"], count=e["runs"], exact_repeat=len(e["exacts"]) == 1,
                               projects=e["projects"], key=key))
    # prefer maximal sequences: drop a candidate contained in a longer one with the same count
    cands.sort(key=lambda c: (-len(c.steps), -c.count))
    kept: list[Candidate] = []
    for c in cands:
        if not any(c.name in k.name and c.count <= k.count for k in kept):
            kept.append(c)
    return kept


SOP_CALL_TOKENS = 150       # the model's output for one SOP call (the call and a line of text)
SOP_INTERFACE_TOKENS = 150  # reading an SOP's tool description to use it, before the SOP exists to measure
TOKENS_PER_SECOND = 100.0   # generation speed, for estimates where no timing was measured


def estimate_savings(c: Candidate, turn_costs: dict | None = None) -> dict:
    """What replacing one repetition of ``c`` with one SOP call saves, per use: the tokens the model spends
    producing the steps, less what using the SOP costs it (reading its interface and writing the call).
    Measured from the turns that produced the repetitions when their cost is known (mid-run); otherwise
    estimated from the size of the steps."""
    use_cost = SOP_CALL_TOKENS + SOP_INTERFACE_TOKENS
    if turn_costs and c.occurrences:
        per = []
        for occ in c.occurrences:
            turns = {s.get("turn") for s in occ if s.get("turn") is not None}
            if turns and all(t in turn_costs for t in turns):
                per.append((sum(turn_costs[t]["tokens"] for t in turns), sum(turn_costs[t]["seconds"] for t in turns),
                            len(turns)))
        if per:
            tokens = sum(p[0] for p in per) / len(per)
            seconds = sum(p[1] for p in per) / len(per)
            turn_s = sum(p[1] for p in per) / max(1, sum(p[2] for p in per))     # one turn, on average
            return {"tokens": int(tokens - use_cost), "seconds": round(max(0.0, seconds - turn_s), 1),
                    "measured": True, "cost_tokens": int(tokens), "cost_seconds": round(seconds, 2)}
    tokens = sum(len(json.dumps(s.get("input", {}))) // 4 + SOP_CALL_TOKENS for s in c.steps) - use_cost
    return {"tokens": int(tokens), "seconds": round(max(0.0, tokens / TOKENS_PER_SECOND), 1), "measured": False}


def measured_savings(occurrences: list[tuple[list[dict], dict]]) -> dict | None:
    """Savings per use measured from the runs the repetitions came from: each run records what every model turn
    cost (output tokens, seconds), and a repetition cost the turns that produced its steps."""
    per = []
    for steps, run in occurrences:
        costs = {int(k): v for k, v in ((run or {}).get("turn_costs") or {}).items()}
        turns = {st.get("turn") for st in steps if st.get("turn") is not None}
        if turns and all(t in costs for t in turns):
            per.append((sum(costs[t]["tokens"] for t in turns), sum(costs[t]["seconds"] for t in turns), len(turns)))
    if not per:
        return None
    tokens = sum(x[0] for x in per) / len(per)
    seconds = sum(x[1] for x in per) / len(per)
    turn_s = sum(x[1] for x in per) / max(1, sum(x[2] for x in per))
    return {"tokens": int(tokens - SOP_CALL_TOKENS - SOP_INTERFACE_TOKENS),
            "seconds": round(max(0.0, seconds - turn_s), 1), "measured": True,
            "cost_tokens": int(tokens), "cost_seconds": round(seconds, 2)}


TEST_RULES = ("Tests must be self-contained and runnable as written: each runs in a fresh empty folder, so give any "
              "input file as a fixture, \"files\": {\"relative/path\": \"text content\"}, and build anything else "
              "(binary files such as images or archives) with \"setup\": \"<python that writes them in the current "
              "folder>\"; inputs refer to fixtures by relative path. No absolute paths, placeholders or files that "
              "do not exist. Expected values must be what the script really produces for that input.")


REVIEW_SYSTEM = ("You review your own recent agent runs and point out the multi-step tasks you repeated, so they can "
                 "become reusable standard procedures. Output JSON only.")

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
unavoidable (no standard way exists), reply {"keep": true, "why": "..."}. Otherwise reply {"script": "..."} with the complete new Python source.
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
            f"Current {sop.entry}:\n{src[:12000]}"), max_tokens=8000, schema=schemas.SOP_SIMPLIFIED)
    except Exception as e:
        return {"simplified": False, "reason": f"rewrite failed: {e}"}
    if not isinstance(r, dict) or r.get("keep") or not isinstance(r.get("script"), str):
        return {"simplified": False, "reason": "kept: " + str((r or {}).get("why", "no rewrite offered"))[:200]}
    tmp = _write_temp(r["script"])
    try:
        _check_compiles(tmp)
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


class CloseSOPExists(Exception):
    """A new procedure is close to an existing SOP (its id is the message), and extending that SOP failed."""


def _check_compiles(path) -> None:
    """Raise SyntaxError if the script does not compile. The builtin compile writes no .pyc, so a shared
    __pycache__ owned by another user cannot make a good script look broken."""
    compile(Path(path).read_text(errors="replace"), str(path), "exec")


def _write_temp(source: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".py", prefix="rameness-simplify-")
    with os.fdopen(fd, "w") as f:
        f.write(source)
    return path


EXTEND_SYSTEM = """You extend an existing SOP script so it also covers a new, similar procedure. Keep everything it
already does working exactly as before: keep every existing input and output; any new input must be optional with a
default that reproduces the old behaviour. Change as little as possible, and use only the Python standard library and
programs every Linux system has unless the existing script already needs more. Reply {"script": the complete Python source,
"inputs": <full JSON schema>, "outputs": {...}, "description": str, "keywords": [..],
"tests": [<new tests for the added behaviour, each {"input": {...}, "expect": {...}}>]}. Each test runs in a fresh
empty folder: give input files as "files": {"relative/path": "text"} and build binary ones with "setup": "<python>";
no absolute paths or placeholders. Output JSON only."""


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
            f"Its code:\n{(spec.get('script') or spec.get('shell') or '')[:8000]}"), max_tokens=10000,
            schema=schemas.SOP_EXTENSION)
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
        if sop.scope != "private":
            keep_out_of_git(lib.private_root)
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


COMPILE_FIX_SYSTEM = """A generated Python SOP script does not compile. Fix only what the compiler error points at
(often a string literal broken by a newline that should be the two characters backslash and n). Keep everything else
exactly as it is. Reply {"script": "..."} with the complete corrected Python source. Output JSON only."""


REPAIR_SYSTEM = """You finish an SOP whose tests fail. You see its script, its tests, and what each test actually
returned. Decide for each failure whether the script or the test is wrong, and fix it: a script bug gets fixed in the
script; a wrong expectation, a missing fixture or an input the script can't read gets fixed in the test. Keep the tests
checking real behaviour with concrete expected values (never delete them or empty their expectations to make them
pass). The script must read a JSON object of arguments from stdin and print one JSON object to stdout. Reply with
"script" (the complete Python source), "tests" (the full list) and "explanation" (one line). Output JSON only."""


def repair_sop(lib: Library, ex: Executor, sop_id: str, failures: list[str], llm, rounds: int = 3) -> list[str]:
    """Show the model what the failing tests actually returned and let it fix the script or the tests, then run
    them again; up to ``rounds`` times. A repair that drops tests or their expected values is refused. Returns the
    failures that remain (empty: the SOP now passes)."""
    def note(outcome: str) -> None:             # every attempt and how it ended, on the SOP itself
        path = lib.get(sop_id).path / "sop.json"
        data = json.loads(path.read_text())
        data.setdefault("origin", {}).setdefault("repair_attempts", []).append(outcome[:300])
        path.write_text(json.dumps(data, indent=2) + "\n")
        lib.reload()

    for _ in range(rounds):
        if not failures:
            return []
        sop = lib.get(sop_id)
        if sop.kind != "script" or sop.entry != "run.py":
            note(f"not repairable: {sop.kind} with entry {sop.entry}")
            return failures
        outputs = getattr(ex, "last_outputs", [])
        try:
            r = llm.complete_json(REPAIR_SYSTEM, (
                f"SOP {sop.id}: {sop.description}\nInputs: {json.dumps(sop.inputs)}\nOutputs: {json.dumps(sop.outputs)}\n"
                f"run.py:\n{(sop.path / 'run.py').read_text()[:12000]}\n\nTests: {json.dumps(sop.tests)[:6000]}\n\n"
                f"Failures: {json.dumps(failures)[:3000]}\nWhat each test returned: {json.dumps(outputs, default=str)[:4000]}"
                f"\n\n{TEST_RULES}"), max_tokens=10000, schema=schemas.SOP_REPAIR, thinking=2048)
        except Exception as e:
            note(f"model call failed: {type(e).__name__}: {e}")
            return failures
        tests = r.get("tests") if isinstance(r, dict) else None
        if not isinstance(tests, list) or not isinstance(r.get("script"), str):
            note("reply had no script or tests")
            return failures
        def checking(ts):                         # tests that assert something about the result
            return sum(1 for t in ts if isinstance(t, dict) and (t.get("expect") or t.get("expect_keys")
                                                                 or t.get("expect_error")))
        if len(tests) < max(1, len(sop.tests) - 1) or checking(tests) < checking(sop.tests):
            note(f"refused: the repair dropped tests or their expectations ({len(sop.tests)} -> {len(tests)} tests, "
                 f"{checking(sop.tests)} -> {checking(tests)} that check something)")
            return failures                      # a repair that guts the tests is not a repair
        tmp = _write_temp(r["script"])
        try:
            _check_compiles(tmp)
        except Exception as e:
            note(f"repaired script does not compile: {str(e)[:200]}")
            continue
        finally:
            os.unlink(tmp)
        before_script = (sop.path / "run.py").read_text()
        before_json = (sop.path / "sop.json").read_text()          # restored as written, notes included
        (sop.path / "run.py").write_text(r["script"])
        data = json.loads(before_json)
        data["tests"] = tests
        why = str(r.get("explanation", ""))[:200]
        data.setdefault("origin", {}).setdefault("repairs", []).append(why)
        (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
        lib.reload()
        new_failures = ex.test(sop_id)
        if len(new_failures) > len(failures):          # worse than before: put it back
            (sop.path / "run.py").write_text(before_script)
            (sop.path / "sop.json").write_text(before_json)
            lib.reload()
            ex.test(sop_id)
            note(f"reverted: {len(failures)} -> {len(new_failures)} failing ({why[:150]})")
            continue
        note(f"repair: {len(failures)} -> {len(new_failures)} failing ({why[:150]})")
        failures = new_failures
    return failures


def register_sop(lib: Library, ex: Executor, spec: dict, origin: dict | None = None,
                 root: Path | None = None, jev: Jev | None = None, org=None, llm=None) -> tuple[SOP, list[str]]:
    """Write an SOP to the private library, compile + test it, set its status."""
    sop_id = re.sub(r"[^a-z0-9_.]", "_", spec["id"].lower()).strip("._")
    if "." not in sop_id:
        sop_id = "learned." + sop_id
    root = root or lib.private_root
    keep_out_of_git(root)
    path = root.joinpath(*sop_id.split("."))
    if path.exists() and (path / "sop.json").exists():
        sop_id += "_" + uuid.uuid4().hex[:4]
        path = root.joinpath(*sop_id.split("."))
    tmp = Path(tempfile.mkdtemp(prefix="rameness-sop-"))
    try:
        entry = "run.sh" if spec.get("shell") else "run.py"
        (tmp / entry).write_text(spec.get("shell") or spec["script"])
        if entry == "run.py":
            for attempt in range(3 if llm is not None else 1):
                try:
                    _check_compiles(tmp / entry)
                    break
                except SyntaxError as e:
                    if attempt == 2 or llm is None:
                        raise
                    # a script that does not compile (often JSON vs Python escaping): show the model the error
                    fixed = llm.complete_json(COMPILE_FIX_SYSTEM, (
                        f"Compiler error:\n{str(e)[-800:]}\n\nScript:\n{(tmp / entry).read_text()[:14000]}"),
                        max_tokens=12000, schema=schemas.SOP_SIMPLIFIED, thinking=1024)
                    if not isinstance(fixed, dict) or not isinstance(fixed.get("script"), str):
                        raise
                    (tmp / entry).write_text(fixed["script"])
                    spec = {**spec, "script": fixed["script"]}
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
    if failures and sop.tests and llm is not None:
        from .deps import requirements
        failures = repair_sop(lib, ex, sop_id, failures, llm)    # the model finishes the SOP and its tests
        fresh = lib.get(sop_id)
        fresh.requirements = {k: v for k, v in requirements(fresh.path).items() if v}
        fresh.save()
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
                 min_repeats: int = 2, threshold: float = 0.6, auto_generate: bool = True, org=None,
                 min_saved: int = 500, creation_cost: int = 4000, review_n: int = 5):
        self.lib, self.ex, self.jev, self.runs, self.llm = lib, ex, jev, runs, llm
        self.min_saved, self.creation_cost = min_saved, creation_cost   # see learning.min_tokens_saved_per_use
        self.review_n = review_n                                         # runs the end-of-run review reads
        self.org = org
        self.min_repeats = min_repeats
        self.threshold = threshold
        self.auto_generate = auto_generate

    # ---- candidate discovery

    @staticmethod
    def _listing(steps: list[dict], limit: int = 150) -> list[str]:
        """A compact, numbered listing of a run's steps (the most recent ``limit``) for the model to read."""
        start = max(0, len(steps) - limit)
        out = []
        for i, st in enumerate(steps[start:], start):
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            what = inp.get("command") or inp.get("path") or json.dumps(inp)
            out.append(f"  {i}. {st['tool']}: {str(what)[:160]}" + ("" if st.get("ok", True) else "  [failed]"))
        return out

    def review_runs(self, runs: list[dict]) -> list[Candidate]:
        """The model reads its recent runs (this one and the ones before it) and names the multi-step tasks it
        repeated, pointing at the steps of each repetition. It identifies; Kev and the tests decide the rest."""
        if not self.llm or not runs:
            return []
        labels = {f"R{k + 1}": r for k, r in enumerate(runs)}
        text = "\n\n".join(f"{lab} ({'this run' if k == len(runs) - 1 else 'earlier'}; task: {r.get('task', '')[:160]})\n"
                            + "\n".join(self._listing(r.get("steps") or []))
                            for k, (lab, r) in enumerate(labels.items()))
        try:
            data = self.llm.complete_json(REVIEW_SYSTEM, (
                f"Your most recent runs, oldest first:\n\n{text}\n\n"
                "Which multi-step tasks did you carry out more than once, in one run or across runs, that a reusable "
                "standard procedure could do next time? Count something as the same task even when it ran on other "
                "files or for another language (e.g. syntax-checking Python and then Rust). Leave out single commands, "
                "writing or editing the project's own content, exploration (reading or searching code), calls to "
                "existing SOPs, and re-checks of what an SOP returned (its tests already verify it). For each give "
                '{"name": snake_case, "description": generic one-liner, "params": [what changes between repetitions], '
                '"occurrences": [{"run": "R1", "steps": [step numbers]}, ...]}. Reply {"procedures": [...]}: at most '
                'the three clearest, and an empty list is a good answer when nothing qualifies.'),
                max_tokens=6000, schema=schemas.REVIEW, thinking=2048)
        except Exception:
            return []
        out = []
        for proc in (data or {}).get("procedures", []) if isinstance(data, dict) else []:
            occs = []
            for o in proc.get("occurrences") or []:
                run = labels.get(str(o.get("run")))
                steps = (run or {}).get("steps") or []
                idx = sorted({i for i in o.get("steps") or [] if isinstance(i, int) and 0 <= i < len(steps)})
                if idx:
                    occs.append(([steps[i] for i in idx], run))
            if len(occs) < 2 or max(len(st) for st, _ in occs) < 2:
                continue                                # repeated, and more than a single command
            longest = max(occs, key=lambda o: len(o[0]))[0]
            exact_once = len({tuple(exact(st) for st in o) for o, _ in occs}) == 1   # literally the same commands
            c = Candidate(proc.get("name", "procedure"), proc.get("description", ""), longest,
                          count=len(occs), params=proc.get("params") or [], exact_repeat=exact_once,
                          occurrences=[st for st, _ in occs],
                          projects={r.get("project") for _, r in occs if r.get("project")})
            c.saves = measured_savings(occs) or estimate_savings(c)
            out.append(c)
        return out

    def candidates(self, task: str, steps: list[dict]) -> list[Candidate]:
        """End of a run: the model reviews this run and the four before it for repeated multi-step tasks,
        and Kev scores how reusable each one is."""
        recent = self.runs.all()[-self.review_n:]
        cands = self.review_runs(recent)
        if not cands:
            return []
        d = self.jev.activate("Which of these steps is a standard, reusable procedure likely to recur in future tasks?",
                              task, [Option(str(i), c.text) for i, c in enumerate(cands)])
        for i, c in enumerate(cands):
            p_freq = 1 - math.exp(-(c.count - 1)) if c.count > 1 else 0.0
            c.score = 1 - (1 - d.probs[str(i)]) * (1 - p_freq)
        return sorted(cands, key=lambda c: -c.score)

    def _duplicate(self, c: Candidate) -> str | None:
        """An existing SOP that already does this procedure, unchanged. Kev shortlists by description, then
        confirms each shortlisted SOP pairwise against the procedure's actual steps and the SOP's own code: a
        description-level match alone ("check", "verify") is not a duplicate."""
        existing = list(self.lib.sops.values())
        if not existing:
            return None
        cmds = [str(s["input"].get("command")) for s in c.steps if isinstance(s.get("input"), dict)
                and s["input"].get("command")]
        if cmds and len(cmds) == len(c.steps):     # literally the same commands as an existing SOP's script
            for s in existing:
                code = "".join(f.read_text(errors="replace") for f in s.path.glob("run.*") if f.is_file()) \
                    if s.path.exists() else ""
                if code and all(cmd in code for cmd in cmds):
                    return s.id
        d = self.jev.activate("Does an existing SOP already perform this procedure?", c.text,
                              [Option(s.id, s.text, desc=s.desc) for s in existing])
        by_id = {s.id: s for s in existing}
        steps = "\n".join(self._listing(c.steps, limit=20))
        for sid, p in d.top(3):
            if p < 0.5:
                continue
            state = (f"Procedure: {c.name}: {c.description}\nIts steps:\n{steps}\n\n"
                     f"Existing SOP: {by_id[sid].digest()}\nInputs: {json.dumps(by_id[sid].inputs)}")
            same = self.jev.yes("Does the existing SOP already do this whole procedure, as it is, so the procedure "
                                "needs no new SOP?", state,
                                "same procedure already does it covers every step identical purpose",
                                "different purpose only shares a word or one step partial unrelated",
                                yes_desc="Yes: the existing SOP already performs this procedure.",
                                no_desc="No: it does something else, or only part of it.")
            if same >= 0.8:
                return sid
        return None

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

    def extend_or_register(self, spec: dict, origin: dict | None = None, root: Path | None = None,
                           new_if_close: bool = True) -> tuple[SOP, list[str], bool]:
        """Extend a close existing SOP when that works; otherwise register ``spec`` as a new SOP.
        Returns (sop, test failures, extended). With ``new_if_close=False`` (end-of-run learning) a procedure
        close to an existing SOP, whose extension failed and which fails its own tests too, is not kept as a
        near-duplicate: CloseSOPExists. One that passes is kept, in case the closeness call was wrong."""
        target = self.extension_target(spec)
        if target is not None:
            extended = extend_sop(self.lib, self.ex, target, spec, self.llm, origin)
            if extended is not None:
                return extended, [], True
        sop, failures = register_sop(self.lib, self.ex, spec, origin, root=root, jev=self.jev, org=self.org,
                                     llm=self.llm)
        if target is not None and not new_if_close and failures and failures != ["no tests provided"]:
            shutil.rmtree(sop.path, ignore_errors=True)
            self.lib.reload()
            raise CloseSOPExists(target.id)
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
                '["fs:read","fs:write","network","exec","side-effect"], "script": the complete Python source as one string (it '
                'reads a JSON object of arguments from stdin and prints one JSON object), '
                '"tests": [{"input": {...}, "expect": {"output_field": expected_value}}]}. '
                'Tests must assert concrete output values for normal and edge cases, not just output keys. '
                'Error cases may use "expect_error": true. ' + TEST_RULES),
                max_tokens=16000, schema=schemas.SOP_SPEC)   # thinking + the script + tests with their fixtures
        except Exception as e:
            self.last_spec_error = f"{type(e).__name__}: {str(e)[:200]}"
            return None

    def observe(self, task: str, steps: list[dict], success: bool, extra: dict | None = None,
                review: bool = True) -> list[dict]:
        """After a run: save it to the history, then (``review``) have the model look for tasks it repeated."""
        self.runs.save(task, steps, success, extra)
        if not success or not steps or not review:
            return []
        return self._learn(task, self.candidates(task, steps))

    def _root_for(self, c: Candidate) -> Path | None:
        """Repeats seen in two or more projects go to the user's own library, so every project can use them;
        repeats from one project (or one run) stay with that project."""
        roots = [r for r, scope in self.lib.roots if scope == "private"]
        return roots[0] if len(c.projects) >= 2 and len(roots) >= 2 else None

    def _learn(self, task: str, cands: list[Candidate], midrun: bool = False) -> list[dict]:
        created = []
        runs_total = sum(1 for r in self.runs.all() if r.get("success"))   # usage evidence for builtin vs registry
        for c in cands:
            if c.score < self.threshold:
                break
            c.saves = c.saves or estimate_savings(c)
            # Worth it? A short procedure is quick for the model to write again; an SOP must save a real amount
            # per use, and over the uses we expect (as many again) more than generating and testing it costs.
            if (self.min_saved > 0 and c.saves["tokens"] < self.min_saved) or \
                    (self.creation_cost > 0 and c.saves["tokens"] * c.count < self.creation_cost):
                created.append({"candidate": c.name, "skipped": "too small to be worth an SOP",
                                "saves_per_use": c.saves})
                continue
            dup = self._duplicate(c)
            if dup:
                created.append({"candidate": c.name, "skipped": f"duplicates {dup}", "existing": dup})
                continue
            spec = self._deterministic_spec(c) or (self._llm_spec(task, c) if self.auto_generate else None)
            if not spec:
                why = getattr(self, "last_spec_error", None) if self.llm else None
                self.last_spec_error = None
                created.append({"candidate": c.name, "score": round(c.score, 2),
                                "skipped": f"generation failed: {why}" if why else "no generator available"})
                continue
            dup = self._duplicate_generated(spec)
            if dup:
                created.append({"candidate": c.name, "skipped": f"generated procedure duplicates {dup}",
                                "existing": dup})
                continue
            origin = {"task": task[:200], "score": c.score, "runs_seen": 0 if midrun else c.count,
                      "saves_per_use": c.saves,
                      "runs_total": runs_total, **({"repeats_in_run": c.count} if midrun else {}),
                      **({"projects": len(c.projects)} if c.projects else {})}
            try:
                sop, failures, extended = self.extend_or_register(spec, origin, root=self._root_for(c),
                                                                  new_if_close=False)
            except CloseSOPExists as e:
                created.append({"candidate": c.name, "skipped": f"close to {e}; extending it and the new SOP both failed their tests",
                                "existing": str(e)})
                continue
            except Exception as e:
                created.append({"candidate": c.name, "error": str(e)})
                continue
            base = sop.id.rsplit("_", 1)[0]
            if failures and failures != ["no tests provided"] and base != sop.id and \
                    getattr(self.lib.sops.get(base), "status", None) == "validated":
                shutil.rmtree(sop.path, ignore_errors=True)    # a failing copy of a working SOP of the same name
                self.lib.reload()
                created.append({"candidate": c.name, "skipped": f"failing variant of the validated {base}",
                                "existing": base})
                continue
            created.append({"sop": sop.id, "status": sop.status, "visibility": sop.visibility,
                            "score": round(c.score, 2), "failures": failures, "tool": sop.tool_name,
                            "repeats": c.count,
                            **({"extended": sop.version} if extended else {})})
            if len(created) >= 3:
                break
        return created
