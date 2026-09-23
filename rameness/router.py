"""Task triage: before any big-model call, the JEV decides how the task runs.

Routes:

* ``direct``  - a validated script SOP covers the task and its arguments can be
                resolved: execute it, no reasoning model at all
* ``answer``  - a question the model can answer without tools: one call, no tool schemas
* ``agent``   - full agent loop, with only the pre-selected SOPs exposed as tools
* ``clarify`` - a relevant SOP category is blocked on missing information that
                neither the task nor the org defaults provide
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .jev import Jev, Option
from .org import Org
from .sops import SOP, Activation, Library, Requirement, activate, validate_args

ROUTES = [
    Option("direct", "run get fetch download list show find locate summarize status check extract count "
                     "lookup retrieve single known procedure"),
    Option("answer", "what why how explain define describe difference meaning concept question compare "
                     "opinion recommend should"),
    Option("agent", "implement build create fix debug refactor add change update write code investigate "
                    "migrate deploy project feature bug error failing modify rename edit generate"),
]

EFFORT = [
    Option("low", "list show get fetch status simple quick rename format lookup count find what"),
    Option("medium", "add update write change summarize explain small script test"),
    Option("high", "design architect debug investigate refactor complex migrate optimize why failing race "
                   "concurrency security performance plan"),
]


@dataclass
class Plan:
    task: str
    route: str
    effort: str
    activation: Activation
    route_probs: dict
    effort_probs: dict
    direct_sop: SOP | None = None
    direct_args: dict | None = None
    questions: list[Requirement] = field(default_factory=list)
    gate: str = "jev"               # jev | director (the user decided) | jev-noted (would have asked, no user present)
    pulls: list[dict] = field(default_factory=list)   # remote-registry pull decisions made for this task

    def describe(self) -> str:
        lines = [f"route:  {self.route}  {fmt(self.route_probs)}  [decided by: {self.gate}]",
                 f"effort: {self.effort}  {fmt(self.effort_probs)}",
                 f"jev calls for SOP traversal: {self.activation.jev_calls}",
                 "traversal:", self.activation.report() or "  (empty library)"]
        if self.activation.selected:
            lines.append("selected SOPs:")
            lines += [f"  {p:.2f}  {s.interface()}" for s, p in self.activation.selected]
        if self.activation.deferred:
            lines.append("deferred (missing information):")
            lines += [f"  {n.id}: needs {', '.join(r.name for r in rs)}" for n, _, rs in self.activation.deferred]
        if self.pulls:
            lines.append("remote registry:")
            lines += [f"  {x['result']:8s} {x['p']:.2f}  {x['id']}  ({x['reason']})" for x in self.pulls]
        if self.direct_sop:
            lines.append(f"direct execution: {self.direct_sop.id}({self.direct_args})")
        return "\n".join(lines)


def decisive(d, default: str, margin: float = 0.08) -> str:
    """The JEV's pick, unless it is effectively a tie - then the safe default."""
    top = d.top(2)
    if len(top) < 2:
        return top[0][0] if top else default
    (a, pa), (_, pb) = top
    return a if pa - pb >= margin else default


def fmt(p: dict) -> str:
    return "(" + ", ".join(f"{k}={v:.2f}" for k, v in sorted(p.items(), key=lambda kv: -kv[1])) + ")"


_URL = re.compile(r"https?://[^\s'\"<>)]+")
_PATHISH = re.compile(r"(?:[\w.-]*/)*[\w.-]+\.[A-Za-z0-9]{1,6}\b")
_GLOB = re.compile(r"\*[\w.*]*|[\w-]*\*\.[\w]+")


def heuristic_args(sop: SOP, task: str, cwd: Path, defaults: dict) -> dict:
    """Resolve obvious arguments without any model call."""
    args: dict = {}
    props = sop.inputs.get("properties", {})
    for name, spec in props.items():
        if name in defaults:
            args[name] = defaults[name]
        elif name == "url" and (m := _URL.search(task)):
            args[name] = m.group(0).rstrip(".,")
        elif name in ("file", "path"):
            for cand in _PATHISH.findall(task):
                if (cwd / cand).exists():
                    args[name] = cand
                    break
        elif name == "pattern" and (m := _GLOB.search(task)):
            args[name] = m.group(0)
    return args


class Router:
    def __init__(self, lib: Library, jev: Jev, org: Org, cfg: dict, cwd: Path, llm=None, confirm=None,
                 remote=None, validator=None):
        self.lib, self.jev, self.org, self.cfg, self.cwd, self.llm = lib, jev, org, cfg, cwd, llm
        # confirm(question, probs, recommended, reason) -> chosen option; None when no user is present
        self.confirm = confirm
        self.remote = remote                 # remote.RemoteRegistry | None
        self.validator = validator           # sop_id -> list of test failures
        self.on_decision = None              # (kind, decision, detail) -> None, for the fleet shadow view

    def _pull_remote(self, task: str, ctx: str, defaults: dict) -> list[dict]:
        """Coverage gap: let JEV pick SOPs from the remote registry, gated and verified."""
        from .remote import PULL_Q, pull_options
        jc, rc = self.cfg["jev"], self.cfg.get("registry") or {}
        allow = set(self.cfg["permissions"]["sop_allow"])
        out = []
        cands = self.remote.candidates(self.jev, task, ctx, defaults, set(self.lib.sops),
                                       jc["activate_threshold"], jc["explore_threshold"], jc["beam"])
        for sop, p in cands:
            q = f"{task[:400]} :: {sop.id} {sop.description}"
            d = self.jev.choose(PULL_Q, q, pull_options(sop, p))
            rec = {"id": sop.id, "p": p, "result": "skipped", "reason": f"JEV: {d.best}"}
            if d.best == "pull":
                extra = sorted(set(sop.permissions) - allow)
                c = self.jev.comfort(PULL_Q, q, d)
                if extra or c.needs_user or rc.get("auto_pull") == "ask":
                    why = f"needs {', '.join(extra)}" if extra else c.reason
                    ans = self.confirm(f"{PULL_Q} {sop.id} - {sop.description}", d.probs, "pull", why) \
                        if self.confirm else None
                    if ans != "pull":
                        rec["reason"] = f"{why}; no user approval"
                        out.append(rec)
                        continue
                    rec["reason"] = f"user approved ({why})"
                else:
                    rec["reason"] = "JEV: pull (routine, within allowed permissions)"
                try:
                    self.remote.fetch(sop.id)
                    self.lib.add_root(self.remote.install_root)
                    failures = self.validator(sop.id) if self.validator else []
                except Exception as e:
                    failures = [f"{type(e).__name__}: {e}"]
                if failures:
                    self.remote.remove(sop.id)
                    self.lib.reload()
                    rec.update(result="rejected", reason="; ".join(failures)[:200])
                else:
                    rec["result"] = "pulled"
            out.append(rec)
            if self.on_decision:
                self.on_decision("pull", d, {"action": rec["result"], "signals": [rec["reason"]]})
        return out

    def resolve_args(self, sop: SOP, task: str, resolved: dict) -> dict | None:
        defaults = {**self.org.defaults, **resolved}
        args = heuristic_args(sop, task, self.cwd, defaults)
        if not validate_args(sop, args):
            return args
        if self.llm is None:
            return None
        # a tiny call that sees one schema - far cheaper than an agent turn
        try:
            got = self.llm.complete_json(
                "Extract tool arguments from a request. Output JSON only.",
                f"Request: {task}\nKnown defaults: {defaults}\nTool: {sop.interface()}\n"
                f"Schema: {sop.inputs}\nReply with the arguments object; use null for anything not stated.",
                max_tokens=1000)
        except Exception:
            return None
        args.update({k: v for k, v in got.items() if v is not None and k in sop.inputs.get("properties", {})})
        return None if validate_args(sop, args) else args

    def plan(self, task: str, resolved: dict | None = None, allow_direct: bool = True) -> Plan:
        resolved = resolved or {}
        jc = self.cfg["jev"]
        org_ctx = self.org.render()
        task_x = self.org.expand(task)
        act = activate(self.lib, self.jev, task_x, org_ctx, {**self.org.defaults, **resolved},
                       jc["activate_threshold"], jc["explore_threshold"], jc["beam"], jc["max_sops"])
        rd = self.jev.choose("How should this task be executed?", task, ROUTES)
        ed = self.jev.choose("How much reasoning does this task need?", task, EFFORT)
        pulls = []
        rc = self.cfg.get("registry") or {}
        best = act.selected[0][1] if act.selected else 0.0
        if (self.remote and rc.get("auto_pull", "gated") != "off" and rd.probs.get("answer", 0) < 0.6
                and best < jc["activate_threshold"] + rc.get("coverage_margin", 0.15)):
            pulls = self._pull_remote(task_x, org_ctx, {**self.org.defaults, **resolved})
            if any(x["result"] == "pulled" for x in pulls):
                act = activate(self.lib, self.jev, task_x, org_ctx, {**self.org.defaults, **resolved},
                               jc["activate_threshold"], jc["explore_threshold"], jc["beam"], jc["max_sops"])
        plan = Plan(task, decisive(rd, "agent"), decisive(ed, "medium"), act, rd.probs, ed.probs, pulls=pulls)
        c = self.jev.comfort("How should this task be executed?", task, rd)
        if c.needs_user:
            if self.confirm:
                plan.route = self.confirm("How should this task be executed?", rd.probs, plan.route, c.reason) or plan.route
                plan.gate = "director"
            else:
                plan.gate = "jev-noted"

        if act.missing:
            plan.route = "clarify"
            plan.questions = act.missing
            return plan

        top = act.selected[0] if act.selected else None
        # several capability areas => a multi-step task; one SOP alone can't finish it
        multi_step = len({s.id.split(".")[0] for s, _ in act.selected}) > 1
        if (allow_direct and top and not multi_step and top[0].kind == "script" and top[0].status == "validated"
                and (plan.route == "direct" or top[1] >= 0.85) and rd.probs["agent"] < 0.5):
            args = self.resolve_args(top[0], task_x, resolved)
            if args is not None:
                plan.route, plan.direct_sop, plan.direct_args = "direct", top[0], args
                return plan
        if plan.route == "direct":
            plan.route = "agent"          # wanted a procedure but couldn't bind one: reason with SOP tools
        if plan.route == "answer" and act.selected and act.selected[0][1] >= 0.7:
            plan.route = "agent"          # a question that clearly needs a tool
        return plan
