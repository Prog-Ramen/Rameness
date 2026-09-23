"""The adaptive agent loop.

    task --> Router.plan (JEV: SOP traversal, route, effort)
         |-- clarify --> questions back to the caller; resume with ``resolved``
         |-- direct  --> Executor.run(SOP)                 (no reasoning-model call)
         |-- answer  --> one model call, no tool schemas
         '-- agent   --> loop: model -> tools -> ContextManager.ingest
                                 checkpoints: compaction (JEV relevance ranking)
         --> Learner.observe(trace)  (JEV + frequency -> new SOPs)
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import jev as jev_mod
from . import llm as llm_mod
from .context import ArtifactStore, ContextManager, estimate_tokens
from .loopguard import LoopGuard, degenerate
from .learning import Learner, RunStore, register_sop
from .org import Org
from .router import Plan, Router
from .sops import SOP, Executor, Library, SOPError
from .tools import BUILTIN_SCHEMAS, Approver, Toolbox

SYSTEM = """You are rameness, an agent that completes software and automation tasks in {cwd}.

Work method:
- Act rather than narrate. Read before you edit. Verify changes (run the tests or the code) before you finish.
- Tools named sop_* are tested standard procedures from the SOP library. Prefer them over re-deriving the same steps.
  If none fits, sop_search may find one; matches become callable on your next turn.
- Large tool outputs are stored and shown as a preview with a ref; use recall(ref, ...) to read more.
  Evicted observations also keep their ref.
- If you work out a deterministic, parameterised procedure that is likely to be needed again, save it with sop_save.
- Ask the user only when a decision is genuinely theirs; otherwise pick the sensible default and say so.
- Finish with a brief summary of what you did and anything left open."""


@dataclass
class Result:
    route: str
    text: str
    plan: Plan
    steps: list[dict] = field(default_factory=list)
    learned: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class Harness:
    def __init__(self, cfg: dict | None = None, llm: llm_mod.Provider | None = None,
                 approver: Approver | None = None, out=None, env=None, workdir: str | None = None,
                 extra_tools: dict | None = None, system_extra: str = ""):
        """``env``/``workdir``: where tools act (fleet environments); state stays in cfg's cwd.
        ``extra_tools``: {name: (schema, handler(args) -> (text, is_error))}."""
        self.cfg = cfg or config_mod.load()
        self.cwd = Path(self.cfg["_cwd"])
        self.home = config_mod.user_home()
        self.state = self.cwd / ".rameness"
        self.out = out or (lambda s: print(s, file=sys.stderr))
        if llm is None:
            try:
                llm = llm_mod.build(self.cfg)
            except Exception as e:           # no SDK / no credentials: still usable for SOPs + planning
                self.out(f"[rameness] no model provider ({e}); running SOP-only")
        self.llm = llm
        self.jev = jev_mod.build(self.cfg, llm, self.state / "decisions.jsonl")
        self.org = Org.load(self.cwd, self.home)
        self.lib = Library.default(self.cwd, self.home)
        self.approver = approver or Approver(self.cfg["permissions"]["mode"])
        self.executor = Executor(self.lib, self.cfg["permissions"]["sop_allow"],
                                 approve=lambda s, a: self.approver(f"sop {s.id} [{', '.join(s.permissions)}]",
                                                                    json.dumps(a)[:400]),
                                 cwd=Path(workdir) if workdir else self.cwd, env=env)
        self.env = env
        self.extra_tools = extra_tools or {}
        self.system_extra = system_extra
        cc = self.cfg["context"]
        self.ctx = ContextManager(ArtifactStore(self.state / "artifacts"), self.jev, cc["budget_tokens"],
                                  cc["offload_chars"], cc["keep_recent_turns"])
        lc = self.cfg["learning"]
        self.learner = Learner(self.lib, self.executor, self.jev, RunStore(self.state / "runs"), llm,
                               lc["min_repeats"], lc["sop_threshold"], lc["auto_generate"], org=self.org)
        self.router = Router(self.lib, self.jev, self.org, self.cfg, self.cwd, llm, confirm=self._confirm)
        self.toolbox = Toolbox(Path(workdir) if workdir else self.cwd, self.approver, env)
        self.messages: list[dict] = []           # persists across run() calls for chat sessions
        self.inbox = None                        # optional callable -> list[str] of messages to inject
        self.on_turn = None                      # optional callable(turn, response) for progress reporting
        self.on_decision = None                  # optional callable(kind, decision, detail) - fleet shadow hook
        self.loop_guard_enabled = self.cfg.get("loop_guard", True)

    def _confirm(self, question: str, probs: dict, recommended: str, reason: str) -> str | None:
        """Comfort-gate prompt in the terminal (only in ask mode with a TTY)."""
        if self.approver.mode != "ask" or not sys.stdin.isatty():
            return None
        opts = sorted(probs, key=lambda k: -probs[k])
        shown = ", ".join(f"{k} {probs[k]:.0%}" for k in opts)
        try:
            ans = self.approver.prompt(f"\n[rameness] JEV would rather you decide ({reason}): {question}\n"
                                       f"  {shown}\n  choose [{'/'.join(opts)}] (enter = {recommended}) > ").strip()
        except EOFError:
            return None
        return ans if ans in opts else recommended

    # ------------------------------------------------------------------ public

    def plan(self, task: str, resolved: dict | None = None) -> Plan:
        return self.router.plan(task, resolved)

    def run(self, task: str, resolved: dict | None = None, allow_direct: bool = True) -> Result:
        t0 = time.time()
        u0 = dict(self.llm.usage) if self.llm else {"input": 0, "output": 0, "calls": 0}
        j0 = self.jev.calls
        plan = self.router.plan(task, resolved, allow_direct=allow_direct and self.cfg.get("allow_direct", True))
        self.out(f"[rameness] route={plan.route} effort={plan.effort} sops="
                 f"{[s.id for s, _ in plan.activation.selected]}")

        if plan.route == "clarify":
            qs = [r.question or f"Which {r.name.replace('_', ' ')} should I use?" for r in plan.questions]
            res = Result("clarify", "\n".join(qs), plan)
        elif plan.route == "direct":
            res = self._direct(plan)
            if res is None:                                  # direct failed: fall back to reasoning
                plan.route = "agent"
                res = self._agent(task, plan)
        elif plan.route == "answer" or self.llm is None:
            res = self._answer(task, plan) if self.llm else Result(
                "none", "No model provider configured and no SOP could run this task directly.", plan)
        else:
            res = self._agent(task, plan)

        if self.cfg["learning"]["enabled"] and res.route in ("agent", "direct"):
            res.learned = self.learner.observe(task, res.steps, res.metrics.get("success", False),
                                               {"route": res.route, "sops": [s.id for s, _ in plan.activation.selected]})
            if res.learned:
                self.out(f"[rameness] learned: {res.learned}")
        u1 = self.llm.usage if self.llm else u0
        res.metrics.update({"seconds": round(time.time() - t0, 2), "jev_calls": self.jev.calls - j0,
                            "llm_calls": u1["calls"] - u0["calls"], "input_tokens": u1["input"] - u0["input"],
                            "output_tokens": u1["output"] - u0["output"],
                            "jev_escalations": getattr(self.jev.backend, "escalations", 0)})
        return res

    # ------------------------------------------------------------------ routes

    def _direct(self, plan: Plan) -> Result | None:
        sop, args = plan.direct_sop, plan.direct_args
        self.out(f"[rameness] direct: {sop.id} {json.dumps(args)[:200]}")
        step = {"tool": sop.tool_name, "input": args}
        try:
            result = self.executor.run(sop.id, args)
        except SOPError as e:
            self.out(f"[rameness] direct execution failed ({e}); falling back to the agent")
            return None
        text = json.dumps(result, indent=2, default=str)
        return Result("direct", self.ctx.ingest(sop.id, text), plan, [{**step, "ok": True}],
                      metrics={"success": True})

    def _system(self, plan: Plan) -> str:
        where = self.toolbox.cwd
        if self.env is not None and getattr(self.env, "kind", "local") != "local":
            where = f"{where} on environment '{self.env.id}' ({self.env.kind}; {self.env.text})"
        parts = [SYSTEM.format(cwd=where)]
        if self.system_extra:
            parts.append(self.system_extra)
        if org := self.org.render():
            parts.append("# Organization context (private)\n" + org)
        skills = [s for s, _ in plan.activation.selected if s.kind == "skill"]
        for s in skills:
            parts.append(f"# Procedure: {s.id}\n{s.instructions}")
        weak = [n for n, p, v in plan.activation.explored if v == "weak"]
        if weak:
            parts.append("Other possibly relevant SOPs (use sop_search to load): " + ", ".join(weak[:12]))
        return "\n\n".join(parts)

    def _answer(self, task: str, plan: Plan) -> Result:
        self.messages.append({"role": "user", "content": task})
        r = self.llm.chat(self._system(plan), self.messages, [], effort=plan.effort, max_tokens=16000)
        self.messages.append({"role": "assistant", "content": r.text, "tool_calls": [], "raw": r.raw})
        return Result("answer", r.text, plan, metrics={"success": True})

    def _agent(self, task: str, plan: Plan) -> Result:
        if self.llm is None:
            return Result("none", "No model provider configured.", plan)
        active: dict[str, SOP] = {s.tool_name: s for s, _ in plan.activation.selected if s.kind != "skill"}
        system = self._system(plan)
        self.messages.append({"role": "user", "content": task})
        steps: list[dict] = []
        text, success = "", False
        guard = LoopGuard(self.jev) if self.loop_guard_enabled else None
        stopped = ""
        for turn in range(self.cfg["max_turns"]):
            for note in (self.inbox() if self.inbox else []):
                self.messages.append({"role": "user", "content": note})
            tools = (BUILTIN_SCHEMAS + [s.tool_schema() for s in active.values()]
                     + [schema for schema, _ in self.extra_tools.values()])
            r = self.llm.chat(system, self.messages, tools, effort=plan.effort)
            self.messages.append({"role": "assistant", "content": r.text, "tool_calls": r.tool_calls, "raw": r.raw})
            if self.on_turn:
                self.on_turn(turn, r)
            if r.text:
                text = r.text
            if not r.tool_calls:
                if guard and (degenerate(r.text) or r.stop_reason == "max_tokens"):
                    guard.observe(r, [])
                    verdict = guard.check(turn, task)
                    if verdict and verdict[0] in ("reorient", "reset"):
                        action, sig, d = verdict
                        self.out(f"[rameness] loop guard: {action} ({'; '.join(sig) or 'truncated reply'})")
                        if self.on_decision:
                            self.on_decision("loop", d, {"action": action, "signals": sig})
                        self.messages[-1]["content"] = " ".join(r.text.split()[:200]) + " [truncated: degenerate]"
                        self.messages[-1].pop("raw", None)
                        if action == "reorient":
                            self.messages.append({"role": "user", "content": LoopGuard.reorientation(task, sig, steps)})
                        else:
                            self.messages = LoopGuard.reset_messages(task, sig, steps)
                        continue
                success = r.stop_reason in ("end_turn", "stop_sequence", "")
                break
            results = []
            for call in r.tool_calls:
                self.out(f"  -> {call.name} {json.dumps(call.input)[:160]}")
                out, err = self._call(call.name, call.input, active)
                results.append((out, err))
                steps.append({"tool": call.name, "input": call.input, "ok": not err, "out": out[:200]})
                self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                      "content": self.ctx.ingest(call.name, out), "is_error": err})
            if guard:
                guard.observe(r, results)
                verdict = guard.check(turn, task)
                if verdict:
                    action, sig, d = verdict
                    self.out(f"[rameness] loop guard: {action} ({'; '.join(sig)})")
                    if self.on_decision:
                        self.on_decision("loop", d, {"action": action, "signals": sig})
                    if action == "reorient":
                        self.messages.append({"role": "user", "content": LoopGuard.reorientation(task, sig, steps)})
                    elif action == "reset":
                        self.messages = LoopGuard.reset_messages(task, sig, steps)
                    elif action == "stop":
                        stopped = "; ".join(sig)
                        break
            # checkpoint: only pay for a JEV relevance pass when the budget is actually under pressure
            if self.ctx.needs_compaction(system, self.messages):
                self.messages = self.ctx.compact(system, self.messages, task)
                self.out(f"[rameness] compacted context -> ~{estimate_tokens(self.messages, system)} tokens")
        else:
            text += "\n[rameness] stopped: max_turns reached"
        if stopped:
            text = f"{text}\n[rameness] stopped by the loop guard: {stopped}".strip()
        return Result("agent", text, plan, steps, metrics={"success": success and not stopped, "turns": turn + 1,
                                                           "loop_interventions": guard.interventions if guard else [],
                                                           "sops_exposed": sorted(s.id for s in active.values())})

    def _call(self, name: str, args: dict, active: dict[str, SOP]) -> tuple[str, bool]:
        if name in self.extra_tools:
            try:
                return self.extra_tools[name][1](args)
            except Exception as e:
                return f"ERROR: {type(e).__name__}: {e}", True
        if name in active:
            try:
                return json.dumps(self.executor.run(active[name].id, args), default=str), False
            except SOPError as e:
                return f"ERROR: {e}", True
        if name == "recall":
            try:
                return self.ctx.store.recall(args["ref"], args.get("start", 1), args.get("end"), args.get("grep")), False
            except KeyError as e:
                return f"ERROR: {e}", True
        if name == "sop_search":
            hits = self.lib.search(self.jev, args["query"])
            for s, _ in hits:
                if s.kind == "skill":
                    continue
                active[s.tool_name] = s
            if not hits:
                return "no matching SOPs", False
            return "\n".join(f"{s.tool_name}: {s.interface()}" + (f"\n{s.instructions}" if s.kind == "skill" else "")
                             for s, _ in hits), False
        if name == "sop_save":
            if not self.approver("sop_save", f"{args.get('id')}: {args.get('description')}"):
                return "DENIED", True
            try:
                sop, failures = register_sop(self.lib, self.executor, args, {"via": "sop_save"},
                                             jev=self.jev, org=self.org)
            except Exception as e:
                return f"ERROR: {e}", True
            active[sop.tool_name] = sop
            msg = f"saved {sop.id} as {sop.status}; callable as {sop.tool_name}"
            return msg + (f"\ntest failures: {failures}" if failures else ""), bool(failures and failures != ["no tests provided"])
        if name.startswith("sop_"):
            return f"ERROR: {name} is not loaded; use sop_search first", True
        return self.toolbox.call(name, args)
