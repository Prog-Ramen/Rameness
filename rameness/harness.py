"""The adaptive agent loop.

    task --> Router.plan (JEV: SOP traversal, route, effort)
         |-- clarify --> questions back to the caller; resume with ``resolved``
         |-- direct  --> Executor.run(SOP)                 (no reasoning-model call)
         |-- answer  --> one model call, no tool schemas
         '-- agent   --> loop: model -> tools -> ContextManager.ingest
                                 checkpoints: compaction (JEV relevance ranking), loop guard,
                                 periodic progress review (JEV: on track / drifting / stalled / finish)
         --> Learner.observe(trace)  (JEV + frequency -> new SOPs)
"""

from __future__ import annotations

import hashlib
import json
import shutil
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import jev as jev_mod
from .jev import Option
from . import llm as llm_mod
from .context import ArtifactStore, ContextManager, estimate_tokens
from .loopguard import LoopGuard, degenerate, error_lines
from . import featurebranch
from .progress import ProgressReview
from .thinking import Thinking
from . import hooks as hooks_mod
from .learning import Learner, RunStore
from .org import Org
from .router import Plan, Router
from .sops import SOP, Executor, Library, SOPError
from .tools import BUILTIN_SCHEMAS, IMAGE_TYPES, Approver, Toolbox

SYSTEM = """You are rameness, an agent that carries out tasks in {cwd}: software, research, analysis, writing,
data and media work.

How to work:
- Act rather than narrate. Keep replies short; put your effort into tool calls.
- Plan first. For any task with three or more steps, write the plan with todo_write before starting. List
  everything the person who asked would expect from the result, each as its own item with the concrete check that
  shows it is done and right. Keep exactly one item in_progress; mark an item completed only when its check has
  passed. A check that covers part of an item does not complete the whole item. Add items as you discover work.
- Work incrementally: get a small, complete version first, then extend it. Keep outputs in focused pieces, and
  write large files in parts (write_file, then edit_file) rather than in a single reply.
- For complex, error-prone parts (parsing, dates and time zones, cryptography, numerical methods, 3D rendering,
  charting...), prefer an established, well-tested library over writing your own, when the task allows it. If the
  result must work without network access, keep a local copy of the library with it.
- Read before you change anything. Create and change files with the file tools (write_file, edit_file), not
  through the shell (heredocs, generator scripts, encodings): the file tools are checked and can be undone.
- Work out early how the result will be judged: how its users will actually use it and how such work is normally
  checked (read relevant sources with web_fetch and inspect the environment and tools where needed). Put those
  checks in your todo list, run them on the first running draft and again after every round of fixes, and read the
  actual output.
- "No errors" is not "done". Check the observable result itself: what its user would see, receive or rely on,
  such as a program's behaviour and output, a report's claims and sources, a dataset's values, or a generated
  file's contents. A check that would also pass on an empty or broken result proves nothing, and a check that
  replaces a part with a stand-in (a stub, mock or fake) says nothing about that part: exercise the parts that
  produce what the user perceives for real, and measure their actual output. Check it from a fresh start, the way
  its user first meets it (clean state, default settings, the normal way in), not only from states you set up.
- Verify and test against real use cases and user experiences. Work out how its users will actually use the result
  (their main tasks and journeys) and exercise those for real, through the same inputs a user would use, over a
  realistic session rather than only the first moment or isolated functions. Look at or read the result at several
  points along the way, and judge it as that user would.
- When something fails, read the error, find the cause, and fix it; do not repeat an identical attempt. Check
  the inputs to the failing part as well as its logic.
- Once a part of the result is verified working, keep that version (commit it, or copy it aside) before changing
  it further. If a later change breaks it, compare against the working version to find what broke, rather than
  debugging from scratch.
- After a fix, re-run the check that exposed the problem and confirm it now passes. A different measurement
  improving is not proof that the problem is gone.
- Do not stop early. Keep going until every part of the task is done and checked, and every todo is completed.
  If time allows, improve quality and fix rough edges.
- Tools named sop_* are tested standard procedures from the SOP library; prefer them when one fits, and call one
  instead of doing its steps by hand first. Its tests already verify its output: do not recompute or re-check what
  it returns; check only what the SOP does not cover. sop_search may find more. Only save a new SOP (sop_save) after the task itself works, and only for a procedure likely to recur.
- Large tool outputs are stored and shown as a preview with a ref; use recall(ref, ...) to read more.
- Search narrowly and read only what you need: grep with a specific pattern (and a glob), leave vendored,
  minified and generated files out, and read the ranges of a large file you need rather than all of it.
- Follow the conventions of what already exists (style, structure, tools). Keep scratch and debug files out of the
  deliverable; put experiments in a temporary directory.
- When several tool calls do not depend on each other, make them in the same reply.
- Ask the user only when a decision is genuinely theirs; otherwise pick the sensible default and say so.
- Finish with a brief summary of what you produced, how you checked it, and anything left open."""

SCOPE_OLD = "  If time allows, improve quality and fix rough edges."
SCOPE_NOTE = ("  Do what was asked, nothing more: unrequested extras add risk, and a mistake in something nobody asked "
              "for still costs trust. Improve quality within the requested scope.")
PROPORTIONAL_NOTE = ("- Match the depth of checking to the task. For a small, well-specified change, run the "
                     "existing tests plus one direct check of each requirement, then finish; keep extended "
                     "verification (new test rigs, fresh-start runs, several angles) for larger or open-ended work.")
INCREMENTAL_OLD = ("- Work incrementally: get a small, complete version first, then extend it. Keep outputs in focused pieces, and\n"
                   "  write large files in parts (write_file, then edit_file) rather than in a single reply.")
DRAFT_NOTE = ("- First a complete running draft, then review-and-fix rounds. As early as you can, write a complete first\n"
              "  draft of the whole result that runs end to end: every part present and connected, even where a part is\n"
              "  still simple. Write each file whole with write_file rather than growing it edit by edit, and do not build\n"
              "  or perfect one feature or module at a time before the whole draft runs. Then work in rounds: review all\n"
              "  the files together, list every issue you find, fix them all in one pass (several edit_file calls in the\n"
              "  same reply), and run the whole result again. Keep each reply well under the output limit: a file too large\n"
              "  for one reply belongs in smaller modules.")
REVIEW_PASS_NOTE = ("- In each round, review before you run: re-read every file against the plan, the task's requirements and\n"
                    "  the other files (names and signatures used across files, data shapes, initialisation order), and\n"
                    "  fix what you find. Check the whole result first, the way its user will use it; use focused checks on\n"
                    "  single parts to locate a problem the whole-result check exposed, not as a stage before the whole runs.\n"
                    "  When a check fails, first confirm the failure is real and not a fault in the check itself, then fix it.")
ONE_TODO_OLD = ("Keep exactly one item in_progress; mark an item completed only when its check has\n"
                "  passed.")
WHOLE_LIST_NOTE = ("Once the plan is set, work to clear the whole list rather than one item at a time:\n"
                   "  build the items together, marking every item you are working on in_progress, and mark each\n"
                   "  completed only when its check has passed.")
BATCH_NOTES = ("- Write down a test plan early: for each requirement, the check that proves it. Once the first draft runs,\n"
               "  automate the plan as one reusable check (a test suite, a validation script, a script that checks a\n"
               "  document's claims against its sources) that runs every check in one go and reports each result. Re-run\n"
               "  it after each round of fixes and extend it as you add requirements. Checks never delay the first running\n"
               "  draft.\n"
               "- After each stage of the work, write one short status line: what is now done and what comes next.\n"
               "- When a check fails, debug in groups: list every anomaly you have seen (including ones you set aside)\n"
               "  and the plausible causes, then test several causes in one probe instead of one per turn. Use Occam's\n"
               "  razor: favour the simplest cause that explains all the anomalies, and check the basics first (is the\n"
               "  data actually there, does the code path run at all, is the value what you expect) before suspecting\n"
               "  libraries, engines or maths.")
TODO_ONE_DESC = "keep one item in_progress"
TODO_WHOLE_DESC = "then work to clear the whole list (several items may be in_progress at once)"
SPECIFIC_NOTE = ("- This task has specific requirements. Put every requirement it states, and every one its user would\n"
                 "  reasonably expect, into the plan and into the first build, and reach them all as soon as you can, each\n"
                 "  proven by your automated checks. When every requirement is met and checked, finish.")
OPEN_ENDED_NOTE = ("- This task is open-ended: there is no fixed list of requirements, and how far the result goes is up to\n"
                   "  you. First get a complete draft running that does what the task names, and review-and-fix it until it\n"
                   "  works and is checked. Then improve it in feature\n"
                   "  cycles ({cycles} in all): in each cycle, come up with new ideas that would most improve the result for\n"
                   "  its user, build the most valuable ones you can finish and test in that cycle, extend your automated\n"
                   "  checks to cover them, and re-run every check. Keep the result working at the end of every cycle.")
FEATURE_CYCLE_NOTE = ("[rameness] Feature cycle {k} of {n}. Before you finish, improve the result further: come up with "
                      "several new ideas that would most improve it for its user (new features, depth, polish, "
                      "robustness), choose the most valuable ones you can build and test in this cycle, add them to "
                      "your todo list with their checks, build them, extend your automated checks to cover them, and "
                      "re-run every check. Everything that worked before must still work.")
FEATURE_BRANCH_NOTE = (" This cycle works on its own copy: git branch rameness/cycle-{n} in {folder}. File tools and shell "
                       "commands that name {root} now act on that copy, so keep using the same paths. The verified "
                       "version in {root} (branch {base}) stays untouched until this cycle passes its checks; then "
                       "Rameness merges it. Servers you started earlier still serve the old folder: start them again "
                       "from the copy before you check anything.")
GROUP_REVIEW_NOTE = ("[rameness] You wrote {n} files since your last review: {files}. Before you rely on them, review "
                     "them together, not one by one: read each against the design and against the others: names and "
                     "signatures used across files, data shapes, units and coordinate conventions, initialisation order, "
                     "and loops or lookups that could silently do nothing (an empty collection, an undefined length, a "
                     "missing key). Fix everything you find in one pass, then run your checks.")
GROUP_DEBUG_NOTE = ("[rameness] You have spent {n} turns investigating without changing the work. Debug as a group now: "
                    "(1) list every anomaly you have observed since this began, including ones you set aside; (2) list "
                    "at least four plausible causes and rank them by Occam's razor: first the simplest cause that would "
                    "explain all the anomalies together, and basic failures (data empty or never produced, code path "
                    "never run, a value not what you assumed) before library, engine or maths faults; (3) write one "
                    "probe that tests all of them together, cheapest checks first, and reports each result; (4) before "
                    "trusting a surprising result, check that the probe itself is right. Then fix what it shows.")
SCRATCH_PARTS = {"test", "tests", "__tests__", "spec", ".scratch", "scratch", "tmp"}
LEDGER_NOTE = ("- Record durable knowledge with note as you go: facts you verified, decisions and why, open issues\n"
               "  (resolve them when fixed), artifacts you produced. Notes and the todo list are kept when older\n"
               "  conversation is trimmed.")

CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".go", ".rs", ".java", ".kt", ".c",
            ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".swift", ".vue", ".svelte", ".sql", ".lua"}


@dataclass
class Result:
    route: str
    text: str
    plan: Plan
    steps: list[dict] = field(default_factory=list)
    learned: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    proposals: list[dict] = field(default_factory=list)


TRUNCATED_NOTE = ("Your last reply was cut off at the output-token limit, so nothing in it was carried out. "
                  "Continue the task. Write each file with write_file; if a file is too large for one reply, split it "
                  "into smaller modules (still a complete draft) rather than printing large content in a reply, and "
                  "keep any reasoning short.")


MID_STEP_OPTIONS = [
    Option("finished", "done complete completed finished verified summary delivered all works ready here is result",
           desc="It finished the work and is reporting the result."),
    Option("mid_step", "let me I'll I will going to next now then colon",
           desc="It announced its next action (e.g. 'Let me fix...') and stopped before taking it."),
]
_TOOL_ATTEMPT = re.compile(r"<tool_call>|<function=|<(read_file|write_file|edit_file|bash|grep|glob|web_fetch)>")


def _wants_tools(text: str) -> bool:
    """A reply that tries to call a tool (in any text form) when none were offered."""
    return bool(_TOOL_ATTEMPT.search(text or ""))


def _overflow(e: Exception) -> bool:
    """A model server refusing a request because it no longer fits the context window."""
    t = str(e).lower()
    return ("context" in t and ("exceed" in t or "too long" in t or "maximum" in t)) or "exceed_context" in t


def code_version() -> str:
    """A short hash of Rameness's own source files, identifying the code a run used."""
    h = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for f in sorted(root.rglob("*.py")):
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


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
        sops_cfg = self.cfg.get("sops") or {}
        private = list(sops_cfg.get("private") or []) + [d for d in os.environ.get("RAMENESS_SOPS", "").split(os.pathsep) if d]
        self.lib = Library.default(self.cwd, self.home, private,
                                   sops_cfg.get("save_to") or (private[-1] if private else None))
        self.approver = approver or Approver(self.cfg["permissions"]["mode"])
        self.executor = Executor(self.lib, self.cfg["permissions"]["sop_allow"],
                                 approve=lambda s, a: self.approver(f"sop {s.id} [{', '.join(s.permissions)}]",
                                                                    json.dumps(a)[:400]),
                                 cwd=Path(workdir) if workdir else self.cwd, env=env)
        self.env = env
        self.extra_tools = extra_tools or {}
        self.system_extra = system_extra
        cc = self.cfg["context"]
        window = cc.get("budget_tokens") or (llm.context_window() if llm else None) or 200000
        self.ctx = ContextManager(ArtifactStore(self.state / "artifacts"), self.jev, window,
                                  cc["offload_chars"], cc["keep_recent_turns"], cc.get("read_chars", 100000),
                                  cc.get("compact_at", 0.93), cc.get("reply_tokens", 32000))
        self.last_context = 0                    # prompt + reply tokens of the last model call, as reported
        lc = self.cfg["learning"]
        # run history is per user (still local), so procedures that recur across projects are noticed
        self.learner = Learner(self.lib, self.executor, self.jev, RunStore(self.home / "runs"), llm,
                               lc["min_repeats"], lc["sop_threshold"], lc["auto_generate"], org=self.org,
                               min_saved=lc.get("min_tokens_saved_per_use", 500),
                               creation_cost=lc.get("sop_creation_tokens", 4000), review_n=lc.get("review_runs", 5))
        rc = self.cfg.get("registry") or {}
        remote = None
        if rc.get("remote_index") and rc.get("auto_pull", "gated") != "off":
            from .remote import RemoteRegistry
            remote = RemoteRegistry(rc["remote_index"], self.home)
        self.router = Router(self.lib, self.jev, self.org, self.cfg, self.cwd, llm, confirm=self._confirm,
                             remote=remote, validator=lambda sid: self.executor.test(sid) if self.lib.get(sid).tests
                             else ["no tests: pulled SOPs must ship tests"])
        self.router.on_decision = lambda *a: self.on_decision(*a) if self.on_decision else None
        self.toolbox = Toolbox(Path(workdir) if workdir else self.cwd, self.approver, env,
                               line_anchors=bool(self.cfg.get("line_anchors")), vision=self._vision(llm),
                               edit_fuzzy=bool(self.cfg.get("edit_fuzzy")))
        self.hooks = hooks_mod.Hooks(self.executor, self.cfg, self.toolbox.cwd, on_event=self.event)
        self.messages: list[dict] = []           # persists across run() calls for chat sessions
        self.todos: list[dict] = []              # the agent's plan (todo_write)
        self.images_seen = False                 # the agent has looked at an image this session (vision_final_look)
        self.ledger: list[dict] = []             # durable task state (note): facts, decisions, issues, artifacts
        self.agent_tag = ""                      # "sub:N" for a delegated sub-agent (event log)
        self.depth = 0                           # sub-agents do not delegate further
        self.delegations = 0
        self._reviewed = False                   # the one self-review before stopping (_unfinished)
        self.inbox = None                        # optional callable -> list[str] of messages to inject
        self.on_turn = None                      # optional callable(turn, response) for progress reporting
        self.on_decision = None                  # optional callable(kind, decision, detail) - fleet shadow hook
        self.loop_guard_enabled = self.cfg.get("loop_guard", True)
        self.progress_cfg = self.cfg.get("progress_review") or {}
        # optional timestamped JSONL of everything the agent loop does (turns, tool calls, JEV actions)
        log = os.environ.get("RAMENESS_EVENT_LOG") or self.cfg.get("event_log")
        self.event_log = Path(log) if log else None
        # full tool inputs (whole files written, every edit) go to their own file so events.jsonl stays small; with
        # both, a run's work directory can be rebuilt as it was at any moment
        tlog = os.environ.get("RAMENESS_TOOL_LOG") or self.cfg.get("tool_log")
        self.tool_log = Path(tlog) if tlog else (self.event_log.with_name("tool_calls.jsonl") if self.event_log else None)

    def event(self, kind: str, **data) -> None:
        if not self.event_log:
            return
        try:
            with self.event_log.open("a") as f:
                f.write(json.dumps({"t": time.time(), "kind": kind, **({"agent": self.agent_tag} if self.agent_tag else {}),
                                **data}, default=str) + "\n")
        except OSError:
            pass

    def log_tool_call(self, turn: int, name: str, args: dict, ok: bool) -> None:
        if not self.tool_log:
            return
        try:
            with self.tool_log.open("a") as f:
                f.write(json.dumps({"t": time.time(), "turn": turn, "tool": name, "ok": ok, "input": args,
                                    **({"agent": self.agent_tag} if self.agent_tag else {})}, default=str) + "\n")
        except OSError:
            pass

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

    def run(self, task: str, resolved: dict | None = None, allow_direct: bool = True,
            allow_clarify: bool = True) -> Result:
        t0 = time.time()
        u0 = dict(self.llm.usage) if self.llm else {"input": 0, "output": 0, "calls": 0}
        j0 = self.jev.calls
        plan = self.router.plan(task, resolved, allow_direct=allow_direct and self.cfg.get("allow_direct", True))
        self.out(f"[rameness] route={plan.route} effort={plan.effort} sops="
                 f"{[s.id for s, _ in plan.activation.selected]}")
        self.event("plan", task=task[:2000], route=plan.route, effort=plan.effort, scope=plan.scope,
                   sops=[s.id for s, _ in plan.activation.selected])
        if plan.route == "agent" or plan.route == "clarify":
            self.hooks.select(self.jev, task)            # JEV: which standard procedures fit this task

        if plan.route == "clarify" and not allow_clarify:      # nobody to ask (auto mode): work with what we have
            plan.route = "agent"
            self.out("[rameness] clarify skipped (non-interactive): proceeding as agent")
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
            if self.llm and _wants_tools(res.text):
                # the model answered by trying to use a tool: the task needs the agent loop after all
                self.messages = self.messages[:-2]
                self.event("route_escalated", frm="answer", to="agent")
                plan.route = "agent"
                res = self._agent(task, plan)
        else:
            res = self._agent(task, plan)

        if self.cfg["learning"]["enabled"] and res.route in ("agent", "direct"):
            res.learned = self.learner.observe(task, res.steps, res.metrics.get("success", False),
                                               {"route": res.route, "sops": [s.id for s, _ in plan.activation.selected],
                                                "project": str(self.cwd),
                                                "turn_costs": getattr(self, "last_turn_costs", {})},
                                               review=res.route == "agent")   # the direct route uses no model
            if res.learned:
                self.out(f"[rameness] learned: {res.learned}")
                for item in res.learned:
                    self.event("sop_learned" if item.get("sop") else "sop_skipped", stage="end", **item)
        rc = self.cfg.get("registry") or {}
        if (rc.get("auto_propose", True) and rc.get("public") and res.metrics.get("success")
                and res.route in ("agent", "direct") and self.cfg["permissions"]["mode"] != "readonly"):
            from .registry import Registry
            registry = Registry(self.cfg, self.home, self.org, self.jev, self.llm)
            try:
                res.proposals = registry.auto_propose(self.lib, self.executor)
            except Exception as e:
                res.proposals = [{"status": "failed", "error": str(e)}]
            for proposal in res.proposals:
                self.event("sop_proposal", **proposal)
                self.out(f"[rameness] SOP proposal: {proposal}")
            if res.proposals:
                path = self.state / "sop-proposals.json"
                try:
                    path.write_text(json.dumps(res.proposals, indent=2) + "\n")
                except OSError as e:
                    self.out(f"[rameness] could not save SOP proposal report: {e}")
        u1 = self.llm.usage if self.llm else u0
        self.event("end", route=res.route, success=res.metrics.get("success"), turns=res.metrics.get("turns"),
                   text=res.text[-4000:])
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
        system = SYSTEM.format(cwd=where)
        if self.cfg.get("state_ledger"):
            system = system.replace("\n- When several tool calls", "\n" + LEDGER_NOTE + "\n- When several tool calls", 1)
        if self.cfg.get("scope_discipline") and SCOPE_OLD in system:
            system = system.replace(SCOPE_OLD, SCOPE_NOTE, 1)
        if self.cfg.get("draft_then_revise") and INCREMENTAL_OLD in system:
            system = system.replace(INCREMENTAL_OLD, DRAFT_NOTE + ("\n" + REVIEW_PASS_NOTE if self.cfg.get("review_pass") else ""), 1)
        if self.cfg.get("batch_workflow") and ONE_TODO_OLD in system:
            system = system.replace(ONE_TODO_OLD, WHOLE_LIST_NOTE, 1)
            system = system.replace("\n- When something fails,", "\n" + BATCH_NOTES + "\n- When something fails,", 1)
        scope = getattr(plan, "scope", "")
        if scope == "open_ended" and self._feature_cycles():
            system = system.replace("\n- When something fails,", "\n" + OPEN_ENDED_NOTE.format(cycles=self._feature_cycles())
                                    + "\n- When something fails,", 1)
        elif scope == "specific" and (self.cfg.get("task_scope") or {}).get("mode", "jev") != "off":
            system = system.replace("\n- When something fails,", "\n" + SPECIFIC_NOTE + "\n- When something fails,", 1)
        if self.cfg.get("proportional_checks"):
            system = system.replace("\n- When something fails,", "\n" + PROPORTIONAL_NOTE + "\n- When something fails,", 1)
        parts = [system, self._environment(where)]
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

    @staticmethod
    def _deliverable(path) -> bool:
        """A change to the work itself, not to a test, probe or scratch file."""
        p = Path(str(path or ""))
        return bool(path) and not (SCRATCH_PARTS & {part.lower() for part in p.parts}) \
            and not any(w in p.name.lower() for w in ("probe", "debug", "test"))

    def _use_branches(self) -> bool:
        """Feature cycles on git branches (task_scope.branches): local work, top-level agent, git installed."""
        return (bool((self.cfg.get("task_scope") or {}).get("branches", True)) and self.depth == 0
                and not self.toolbox.remote and featurebranch.available())

    def _feature_cycles(self) -> int:
        """Feature cycles an open-ended task runs after its core is built (config task_scope.feature_cycles)."""
        ts = self.cfg.get("task_scope") or {}
        return 0 if ts.get("mode", "jev") == "off" else max(0, int(ts.get("feature_cycles", 3)))

    def _vision(self, llm) -> bool:
        """Config "vision": "auto" (ask the provider), true or false."""
        v = self.cfg.get("vision", "auto")
        if v != "auto":
            return bool(v)
        try:
            return bool(llm and llm.supports_vision())
        except Exception:
            return False

    def _prune_images(self) -> None:
        """Images cost ~1-2k tokens each and old ones are stale (the output they show has changed since): keep the
        most recent few in view, and leave a note where the others were."""
        keep = int(self.cfg.get("vision_keep", 2))
        with_imgs = [m for m in self.messages if m.get("images")]
        for m in with_imgs[:max(0, len(with_imgs) - keep)]:
            m.pop("images")
            m["content"] = f"{m['content']} [no longer shown: read the file again to see it now]"

    def _environment(self, where) -> str:
        """What Claude Code tells its model about the machine: enough to pick commands and tools without probing."""
        import datetime
        import platform
        local = self.env is None or getattr(self.env, "kind", "local") == "local"
        cwd = Path(str(self.toolbox.cwd))
        git = local and any((d / ".git").exists() for d in (cwd, *cwd.parents))
        lines = [f"Working directory: {where}", f"Is a git repository: {'yes' if git else 'no'}"]
        if local:
            lines += [f"Platform: {platform.system().lower()} ({platform.machine()})", f"OS version: {platform.release()}"]
        lines.append(f"Today's date: {datetime.date.today().isoformat()}")
        if self.toolbox.vision:
            lines.append("You can see images: read_file on a png, jpg, gif or webp file shows it to you.")
        return "# Environment\n" + "\n".join(lines)

    def _answer(self, task: str, plan: Plan) -> Result:
        self.messages.append({"role": "user", "content": task})
        r = self.llm.chat(self._system(plan), self.messages, [], effort=plan.effort, max_tokens=16000)
        self.messages.append({"role": "assistant", "content": r.text, "tool_calls": [], "raw": r.raw})
        return Result("answer", r.text, plan, metrics={"success": True})

    def _agent(self, task: str, plan: Plan) -> Result:
        if self.llm is None:
            return Result("none", "No model provider configured.", plan)
        active: dict[str, SOP] = {s.tool_name: s for s, _ in plan.activation.selected if s.kind != "skill"}
        self._sop_drafts: list[SOP] = []         # this run's sop_save attempts that failed their tests
        system = self._system(plan)
        # What this run actually used, so a later comparison can tell which prompt and code each run had.
        self.event("system_prompt", sha=hashlib.sha256(system.encode()).hexdigest()[:16], chars=len(system),
                   code=code_version())
        self.messages.append({"role": "user", "content": task})
        steps: list[dict] = []
        turn_costs: dict[int, dict] = {}         # what each model turn cost (output tokens, seconds)
        self.last_turn_costs = turn_costs        # saved with the run, so the end-of-run review can measure savings
        text, success = "", False
        guard = LoopGuard(self.jev) if self.loop_guard_enabled else None
        pc = self.progress_cfg
        review = ProgressReview(self.jev, pc.get("every", 10), pc.get("min_confidence", 0.5)) \
            if pc.get("every", 10) else None
        stopped = ""
        thinking = Thinking.from_cfg(self.jev, self.cfg)
        finish_checks = 0
        cycles_done = 0                          # feature cycles run so far (open-ended tasks, task_scope)
        branches = None                          # featurebranch.FeatureBranches once the first cycle starts
        drafted: list[str] = []                  # code files written whole since the last group review
        investigating = 0                        # turns in a row that ran things without changing the work
        gr, gd = self.cfg.get("group_review") or {}, self.cfg.get("group_debug") or {}
        root_cwd = self.toolbox.cwd
        t_turn_end = 0.0                         # when the previous turn's tools finished (overhead telemetry)
        mid_step_nudges = 0                      # stops right after announcing a step (don't count as finishing)
        todo_turn = 0                            # last turn the agent wrote its todo list
        for turn in range(self.cfg["max_turns"]):
            for note in (self.inbox() if self.inbox else []):
                self.messages.append({"role": "user", "content": note})
            builtin = [t for t in BUILTIN_SCHEMAS if (t["name"] != "edit_lines" or self.toolbox.line_anchors)
                       and (t["name"] != "note" or self.cfg.get("state_ledger"))
                       and (t["name"] != "delegate" or (self.cfg.get("delegation") and self.depth == 0))]
            if self.cfg.get("batch_workflow"):     # the tool's own description must not contradict the prompt
                builtin = [{**t, "description": t["description"].replace(TODO_ONE_DESC, TODO_WHOLE_DESC)}
                           if t["name"] == "todo_write" else t for t in builtin]
            tools = (builtin + [s.tool_schema(self.lib.ok_uses(s.id)) for s in active.values()]
                     + [schema for schema, _ in self.extra_tools.values()])
            # JEV: how hard does the next step need thinking about (local reasoning models don't pace themselves)
            t_decide = time.time()
            level, budget, effort, td = thinking.decide(turn, task, steps, text, plan.effort)
            if td is not None:
                # jev_s: this decision; gap_s: all harness time between the last tool result and this model call
                self.event("thinking", turn=turn, level=level, budget=budget, probs=td.probs,
                           jev_s=round(time.time() - t_decide, 3),
                           gap_s=round(time.time() - t_turn_end, 3) if t_turn_end else None)
            t_call = time.time()
            if self.toolbox.vision and getattr(self.llm, "vision_failed", False):
                self.toolbox.vision = False                  # the server failed on images: read them as files now
                self.event("vision_off", turn=turn, reason="server failed on an image request")
            self._prune_images()
            try:
                r = self.llm.chat(system, self.messages, tools, effort=effort, thinking=budget,
                                  max_tokens=self.ctx.reply_room(system, self.messages, self.last_context))
            except Exception as e:
                if not _overflow(e):
                    raise
                # the server says the request no longer fits: compact hard and try once more
                before = estimate_tokens(self.messages, system)
                self.messages = self.ctx.compact(system, self.messages, task, target=int(self.ctx.limit() * 0.5))
                self._reinject_state("compaction")
                self.out(f"[rameness] context overflow: compacted ~{before} -> ~{estimate_tokens(self.messages, system)} tokens")
                self.event("overflow_recovered", turn=turn, error=str(e)[:300])
                r = self.llm.chat(system, self.messages, tools, effort=effort, thinking=budget,
                                  max_tokens=self.ctx.reply_room(system, self.messages))
            if r.usage.get("input"):
                self.ctx.calibrate(system, self.messages, r.usage["input"])
            self.messages.append({"role": "assistant", "content": r.text, "tool_calls": r.tool_calls, "raw": r.raw,
                                  "reasoning": getattr(r, "reasoning", "")})
            self.last_context = (r.usage.get("input") or 0) + (r.usage.get("output") or 0)
            turn_costs[turn] = {"tokens": int((r.usage or {}).get("output") or 0), "seconds": time.time() - t_call}
            self.event("llm", turn=turn, seconds=round(time.time() - t_call, 2), stop_reason=r.stop_reason,
                       text=(r.text or "")[:4000], reasoning=(getattr(r, "reasoning", "") or "")[:6000],
                       tools=[c.name for c in r.tool_calls], usage=r.usage,
                       messages=len(self.messages))
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
                        self.event("loop_guard", turn=turn, action=action, signals=sig, probs=d.probs)
                        if self.on_decision:
                            self.on_decision("loop", d, {"action": action, "signals": sig})
                        self.messages[-1]["content"] = " ".join(r.text.split()[:200]) + " [truncated: degenerate]"
                        self.messages[-1].pop("raw", None)
                        if action == "reorient":
                            self.messages.append({"role": "user", "content": LoopGuard.reorientation(task, sig, steps)})
                        else:
                            self.messages = LoopGuard.reset_messages(task, sig, steps)
                            self._reinject_state("context reset")
                        continue
                if r.stop_reason != "max_tokens" and turn + 1 < self.cfg["max_turns"] \
                        and mid_step_nudges < self.cfg.get("mid_step_nudges", 10) and self._stopped_mid_step(r.text):
                    # some models announce a step ("Let me fix the condition:") and end the turn without taking it
                    mid_step_nudges += 1
                    self.out("[rameness] stopped mid-step; asking the model to take the step it announced")
                    self.event("mid_step_stop", turn=turn)
                    self.messages.append({"role": "user", "content": "[rameness] You said what you would do next but "
                                          "did not do it. Continue: make that tool call now."})
                    continue
                if r.stop_reason != "max_tokens" and finish_checks < self.cfg.get("finish_checks", 2) \
                        and turn + 1 < self.cfg["max_turns"] and (why := self._unfinished(steps)):
                    # stopping with work visibly left: send it back once or twice, not forever
                    finish_checks += 1
                    self.out(f"[rameness] not finished: {why.splitlines()[0].removeprefix('[rameness] ')}")
                    self.event("finish_check", turn=turn, reason=why)
                    self.messages.append({"role": "user", "content": why})
                    continue
                if r.stop_reason != "max_tokens" and getattr(plan, "scope", "") == "open_ended" \
                        and cycles_done < self._feature_cycles() and turn + 1 < self.cfg["max_turns"]:
                    # open-ended: the finished, reviewed core is the start; improve it for N feature cycles
                    cycles_done += 1
                    finish_checks = 0                # each cycle gets its own finish checks and final review
                    self._reviewed = False
                    n = self._feature_cycles()
                    self.out(f"[rameness] feature cycle {cycles_done} of {n}")
                    self.event("feature_cycle", turn=turn, cycle=cycles_done, of=n)
                    note = FEATURE_CYCLE_NOTE.format(k=cycles_done, n=n)
                    if self._use_branches():
                        try:
                            if branches is None:      # the core just passed its checks: that is the first verified version
                                branches = featurebranch.FeatureBranches(root_cwd)
                                self.event("feature_branch", action="core", base=branches.commit_core())
                            else:                     # the previous cycle just passed its checks
                                msg = branches.merge()
                                self.out(f"[rameness] {msg}")
                                self.event("feature_branch", action="merge", cycle=cycles_done - 1, result=msg)
                            folder = branches.start(cycles_done)
                            self.toolbox.cwd, self.toolbox.branches = folder, branches
                            note += FEATURE_BRANCH_NOTE.format(n=cycles_done, folder=folder, root=branches.root,
                                                               base=branches.base)
                            self.event("feature_branch", action="start", cycle=cycles_done, folder=str(folder))
                        except Exception as e:        # no git, odd repository state: cycles continue in place
                            self.event("feature_branch", action="error", error=str(e)[:300])
                            self.toolbox.cwd, self.toolbox.branches, branches = root_cwd, None, None
                    self.messages.append({"role": "user", "content": note})
                    continue
                if r.stop_reason == "max_tokens" and turn + 1 < self.cfg["max_turns"]:
                    # cut off at the output limit mid-answer: that is not the end of the task
                    self.out("[rameness] reply hit the output limit; asking the model to continue")
                    self.event("truncated", turn=turn)
                    self.messages.append({"role": "user", "content": TRUNCATED_NOTE})
                    continue
                success = r.stop_reason in ("end_turn", "stop_sequence", "")
                break
            if any(c.name == "todo_write" for c in r.tool_calls):
                todo_turn = turn
            results = []
            for call in r.tool_calls:
                self.out(f"  -> {call.name} {json.dumps(call.input)[:160]}")
                t_tool = time.time()
                stub = self._unchanged_read(call, turn)
                self.toolbox.take_images()                 # nothing left over from another call
                out, err = (stub, False) if stub else self._call(call.name, call.input, active)
                images = self.toolbox.take_images()
                self.event("tool", turn=turn, tool=call.name, input=json.dumps(call.input)[:3000], ok=not err,
                           seconds=round(time.time() - t_tool, 2), output=out[:3000], output_chars=len(out))
                self.log_tool_call(turn, call.name, call.input, not err)
                if call.name in active and not err:
                    self._measure_sop_use(active[call.name], turn, turn_costs.get(turn), len(r.tool_calls),
                                          time.time() - t_tool)
                results.append((out, err))
                steps.append({"tool": call.name, "input": call.input, "ok": not err, "out": out[:200],
                              "errs": error_lines(out), "turn": turn})
                msg = {"role": "tool", "tool_call_id": call.id, "name": call.name,
                       "content": self.ctx.ingest(call.name, out), "is_error": err}
                if call.name == "read_file" and not err and not stub:
                    msg["read_key"], msg["read_turn"] = self._read_key(call.input), turn
                if images:
                    msg["images"] = images
                    self.images_seen = True
                    if self.cfg.get("vision_describe_first", True):
                        # asked "does it work?", models confirm what they hope to see; asked what is there, they
                        # report it (occamy v23b: "renders correctly" in-run, "no blocks visible" when asked)
                        msg["content"] += ("\n[rameness] First describe what the image actually shows, element by "
                                           "element; then compare that with what it should show for the task.")
                self.messages.append(msg)
            t_turn_end = time.time()
            if guard:
                guard.observe(r, results)
                verdict = guard.check(turn, task)
                if verdict:
                    action, sig, d = verdict
                    self.out(f"[rameness] loop guard: {action} ({'; '.join(sig)})")
                    self.event("loop_guard", turn=turn, action=action, signals=sig, probs=d.probs)
                    if self.on_decision:
                        self.on_decision("loop", d, {"action": action, "signals": sig})
                    if action == "reorient":
                        self.messages.append({"role": "user", "content": LoopGuard.reorientation(task, sig, steps)})
                    elif action == "reset":
                        self.messages = LoopGuard.reset_messages(task, sig, steps)
                        self._reinject_state("context reset")
                    elif action == "stop":
                        stopped = "; ".join(sig)
                        break
            if review and review.due(turn, steps) and not (guard and turn - guard.last_intervention_turn < 2):
                verdict, d, note = review.review(turn, task, text, steps)
                self.out(f"[rameness] progress review: {verdict} ({d.probs[verdict]:.2f})"
                         + ("" if note else " - no action"))
                self.event("progress_review", turn=turn, verdict=verdict, probs=d.probs, acted=bool(note))
                if self.on_decision:
                    self.on_decision("progress", d, {"action": verdict, "acted": bool(note)})
                if note:
                    self.messages.append({"role": "user", "content": note})
            # group review: a burst of whole-file drafts has ended -> review them together before relying on them
            names = [c.name for c in r.tool_calls]
            for c in r.tool_calls:
                if c.name == "write_file" and Path(str(c.input.get("path", ""))).suffix.lower() in CODE_EXT:
                    drafted.append(str(c.input.get("path")))
            need = gr.get("min_files", 2)
            if need and "write_file" not in names and len(set(drafted)) >= need:
                files = sorted(set(drafted))
                drafted = []
                self.messages.append({"role": "user", "content": GROUP_REVIEW_NOTE.format(
                    n=len(files), files=", ".join(Path(f).name for f in files[:12]))})
                self.event("group_review", turn=turn, files=files)
            # group debugging: many turns that only investigate -> one probe for several causes
            changed = any(c.name in ("write_file", "edit_file", "edit_lines") and self._deliverable(c.input.get("path"))
                          for c in r.tool_calls)
            investigating = 0 if changed else investigating + ("bash" in names)
            first, again = gd.get("after_turns", 6), gd.get("repeat", 12)
            if first and investigating and (investigating == first or
                                             (again and investigating > first and (investigating - first) % again == 0)):
                self.messages.append({"role": "user", "content": GROUP_DEBUG_NOTE.format(n=investigating)})
                self.event("group_debug", turn=turn, turns=investigating)
            every = self.cfg.get("todo_reminder_turns", 30)
            open_ = [t for t in self.todos if t.get("status") != "completed"]
            if every and open_ and turn - todo_turn >= every:
                # as Claude Code does: a plan the agent stopped updating is a plan it stopped following
                todo_turn = turn
                mark = {"in_progress": "[>]", "pending": "[ ]"}
                self.messages.append({"role": "user", "content": (
                    f"[rameness] Reminder: you have not updated your todo list for {every} turns. Open items:\n"
                    + "\n".join(f"{mark.get(t['status'], '[ ]')} {t['content']}" for t in open_)
                    + "\nIf any are done and checked, mark them completed with todo_write, and keep the list "
                      "current as you work.")})
                self.event("todo_reminder", turn=turn, open=len(open_))
            # checkpoint: only pay for a JEV relevance pass when the budget is actually under pressure
            if self.ctx.needs_compaction(system, self.messages, self.last_context):
                before = estimate_tokens(self.messages, system)
                self.messages = self.ctx.compact(system, self.messages, task)
                after = estimate_tokens(self.messages, system)
                self.out(f"[rameness] compacted context -> ~{after} tokens")
                self._reinject_state("compaction")
                self.event("compaction", turn=turn, tokens_before=before, tokens_after=after)
        else:
            text += "\n[rameness] stopped: max_turns reached"
        if stopped:
            text = f"{text}\n[rameness] stopped by the loop guard: {stopped}".strip()
        if branches is not None:
            # the last cycle merges only if the run finished verified; otherwise the project keeps the verified version
            try:
                msg = branches.merge() if success and not stopped else branches.abandon()
                self.event("feature_branch", action="end", result=msg)
                if msg:
                    text = f"{text}\n[rameness] {msg}".strip()
            except Exception as e:
                self.event("feature_branch", action="error", error=str(e)[:300])
            self.toolbox.cwd, self.toolbox.branches = root_cwd, None
        return Result("agent", text, plan, steps, metrics={"success": success and not stopped, "turns": turn + 1,
                                                           "loop_interventions": guard.interventions if guard else [],
                                                           "progress_reviews": review.reviews if review else [],
                                                           "sops_exposed": sorted(s.id for s in active.values())})

    def _delegate(self, args: dict) -> tuple[str, bool]:
        """Run a focused sub-task in a fresh sub-agent, if JEV judges a separate context worth its cost."""
        if self.depth > 0 or not self.cfg.get("delegation"):
            return "ERROR: delegation is not available here; do the sub-task yourself", True
        task, ctx = str(args.get("task", "")).strip(), str(args.get("context", "")).strip()
        if not task:
            return "ERROR: give the sub-task", True
        d = self.jev.choose("Should this sub-task run in a separate sub-agent with a fresh context?",
                            f"Sub-task: {task[:600]}\nContext given: {ctx[:400]}", [
            Option("spawn", "self-contained investigate diagnose research explore many files trial and error "
                            "separate component well specified",
                   desc="Self-contained work that needs a lot of reading or trial and error: a fresh context "
                        "keeps it fast and keeps the main conversation small."),
            Option("inline", "small quick one step depends on current conversation unclear vague",
                   desc="Small, or it depends on the current conversation: cheaper to do directly."),
        ])
        self.event("delegate_decision", task=task[:300], choice=d.best, probs=d.probs)
        if d.best != "spawn":
            return ("Not delegated: this sub-task is small or depends on the current conversation, so do it "
                    "yourself here."), False
        from .tools import SKIP_DIRS
        self.delegations += 1
        child = Harness({**self.cfg, "max_turns": self.cfg.get("delegate_max_turns", 40)}, llm=self.llm,
                        approver=self.approver, out=self.out, env=self.env, workdir=str(self.toolbox.cwd))
        child.depth, child.agent_tag = self.depth + 1, f"sub:{self.delegations}"
        child.event_log = self.event_log
        child.tool_log = self.tool_log
        before = hooks_mod.snapshot(self.toolbox.cwd, SKIP_DIRS)
        brief = (f"{task}\n\nContext from the main agent:\n{ctx}" if ctx else task) + (
            "\n\nYou are a sub-agent working on one part of a larger task. Do this part, check it, and finish "
            "with a short report: what you found or built, how you checked it, and anything left open.")
        self.event("delegate_start", task=task[:300])
        plan = child.router.plan(brief, allow_direct=False)
        plan.route = "agent"
        child.hooks.selected = self.hooks.selected
        res = child._agent(brief, plan)
        changed = [str(Path(p).relative_to(self.toolbox.cwd)) for p in
                   hooks_mod.changed(before, hooks_mod.snapshot(self.toolbox.cwd, SKIP_DIRS))]
        self.event("delegate_end", turns=res.metrics.get("turns"), success=res.metrics.get("success"),
                   changed=changed[:20])
        notes = child.state_summary(1500)
        return (f"Sub-agent report ({res.metrics.get('turns')} turns):\n{res.text[-3000:]}"
                + (f"\nFiles it changed: {', '.join(changed[:20])}" if changed else "")
                + (f"\nIts notes:\n{notes}" if notes else "")), False

    def _note(self, args: dict) -> tuple[str, bool]:
        if args.get("resolve") is not None:
            for n in self.ledger:
                if n["id"] == args["resolve"] and n["kind"] == "issue":
                    n["resolved"] = True
                    self._save_ledger()
                    return f"issue {n['id']} resolved", False
            return f"ERROR: no open issue {args['resolve']}", True
        kind, text = args.get("kind"), " ".join(str(args.get("text", "")).split())[:400]
        if kind not in ("fact", "decision", "issue", "artifact") or not text:
            return "ERROR: give kind (fact | decision | issue | artifact) and text, or resolve=<issue id>", True
        n = {"id": len(self.ledger) + 1, "kind": kind, "text": text}
        self.ledger.append(n)
        self._save_ledger()
        self.event("note", id=n["id"], note_kind=kind, text=text)
        return f"noted {kind} {n['id']}", False

    def _save_ledger(self) -> None:
        try:
            (self.state / "ledger.json").write_text(json.dumps(self.ledger, indent=1))
        except OSError:
            pass

    def state_summary(self, limit: int = 3000) -> str:
        """The task's durable state, compact: re-injected when the conversation is trimmed or reset."""
        parts = []
        open_todos = [t for t in self.todos if t.get("status") != "completed"]
        done = [t for t in self.todos if t.get("status") == "completed"]
        if self.todos:
            rest = ("Open: " + "; ".join(t["content"] for t in open_todos)) if open_todos else "All done."
            parts.append(f"Plan: {len(done)}/{len(self.todos)} done. {rest}")
        for kind, title in (("issue", "Open issues"), ("decision", "Decisions"), ("fact", "Verified facts"),
                            ("artifact", "Artifacts")):
            items = [n for n in self.ledger if n["kind"] == kind and not n.get("resolved")]
            if items:
                parts.append(title + ":\n" + "\n".join(f"- [{n['id']}] {n['text']}" for n in items[-12:]))
        text = "\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n..."

    def _reinject_state(self, why: str) -> None:
        s = self.state_summary()
        if s:
            self.messages.append({"role": "user", "content": f"[rameness state, kept across the {why}]\n{s}"})
            self.event("state_reinjected", why=why, chars=len(s))

    def _read_key(self, args: dict) -> tuple | None:
        """What a read_file call saw: path, range and the file's current content hash."""
        import hashlib
        try:
            p = self.toolbox._p(args["path"])
            digest = hashlib.sha1(p.read_bytes()).hexdigest()
        except (OSError, KeyError):
            return None
        return (str(p), args.get("offset", 1), args.get("limit", 2000), digest, self.toolbox.line_anchors)

    def _unchanged_read(self, call, turn: int) -> str | None:
        """A re-read of an unchanged file whose earlier output is still in the conversation gets a short stub
        instead of the whole file again (as Claude Code's Read does): the context stays smaller, which is speed."""
        if call.name != "read_file" or not self.cfg.get("dedup_reads") or \
                (self.env is not None and getattr(self.env, "kind", "local") != "local"):
            return None
        key = self._read_key(call.input)
        if key is None:
            return None
        image = Path(str(call.input.get("path", ""))).suffix.lower() in IMAGE_TYPES
        for m in reversed(self.messages):
            if m.get("read_key") == key and not m.get("evicted") and "ref=" not in str(m.get("content", ""))[:400] \
                    and (m.get("images") or not image):             # a pruned image is no longer in view
                self.event("read_dedup", path=str(call.input.get("path")), earlier_turn=m.get("read_turn"))
                return (f"[unchanged] {call.input.get('path')} has not changed since you read it at turn "
                        f"{m.get('read_turn')}; that output is still above in this conversation.")
        return None

    def _stopped_mid_step(self, text: str) -> bool:
        """JEV: did a reply without tool calls finish the work, or stop right after announcing its next step?"""
        tail = " ".join((text or "").split())[-400:]
        if not tail:
            return True                          # an empty reply is not a finished answer (no result, no summary)
        facts = [f"The agent's reply ended without a tool call. Its last words: {tail!r}"]
        if tail.rstrip().endswith(":"):
            facts.append("The reply ends with a colon, as if introducing an action.")
        d = self.jev.choose("Did the agent finish its work, or stop partway through a step it had just announced?",
                            "\n".join(facts), MID_STEP_OPTIONS)
        self.event("stop_kind", verdict=d.best, probs=d.probs)
        return d.best == "mid_step" and d.probs.get("mid_step", 0) >= self.cfg.get("mid_step_confidence", 0.5)

    def _unfinished(self, steps: list[dict]) -> str | None:
        """Why the agent should not stop yet: open todos, or code changed since it last ran anything."""
        open_ = [t["content"] for t in self.todos if t.get("status") != "completed"]
        if not open_ and not self._reviewed and (self.todos or len(steps) >= 5):
            # also without a todo list: a model that skipped planning must not skip the review too
            self._reviewed = True
            look = (" You can see images: before this review, look at the final result the way its user would see it "
                    "(capture it fresh from a fresh start and read the image), describe what it shows, and compare it "
                    "with the task." if self.toolbox.vision and self.images_seen
                    and self.cfg.get("vision_final_look", True) else "")
            if self.cfg.get("review_scaled") and len(steps) < self.cfg.get("review_full_steps", 25):
                # a short run: a quick confirmation, not the full audit (A/B review-scaled)
                return ("[rameness] Before you finish, re-read the original task and confirm that every item it asks "
                        "for is done and was checked. If something is missing or unchecked, do it now; otherwise "
                        "finish with a brief summary.")
            return ("[rameness] Before you finish, review the work against the task." + look + " Re-read the original task, list "
                    "everything its user would expect from the result, and for each item say how you verified it "
                    "(a tested SOP that produced it counts as verified; otherwise the check you ran, what it showed, whether it exercised the real thing or a stand-in, and whether "
                    "it started from a fresh start the way its user would). "
                    "Then, for each check, name a flaw its user would notice (wrong, missing or mixed-up content, "
                    "not just an empty result) and say whether that check would have caught it; where it would not, "
                    "run a sharper check. Anything not verified, or verified only in part, goes back on the todo "
                    "list: continue with it. If everything is verified, finish.")
        if open_:
            return ("[rameness] You stopped, but your todo list still has open items:\n"
                    + "\n".join(f"- {t}" for t in open_)
                    + "\nContinue with them. If one is actually done and checked, mark it completed with todo_write; "
                      "if it cannot be done, say why.")
        last_run = max((i for i, s in enumerate(steps) if s["tool"] == "bash"), default=-1)
        edited = sorted({str(s["input"].get("path", "")) for s in steps[last_run + 1:]
                         if s["tool"] in ("write_file", "edit_file", "edit_lines") and s.get("ok", True)
                         and Path(str(s["input"].get("path", ""))).suffix.lower() in CODE_EXT})
        if edited:
            return ("[rameness] You changed code after your last run or test (" + ", ".join(edited[:6]) + "). "
                    "Run or test the final version the way a user would, fix anything that fails, then finish.")
        return None

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
        if name == "note":
            return self._note(args)
        if name == "delegate":
            return self._delegate(args)
        if name == "todo_write":
            self.todos = [{"content": str(t.get("content", "")), "status": t.get("status", "pending")}
                          for t in args.get("todos") or [] if isinstance(t, dict)]
            self.event("todos", todos=self.todos)
            mark = {"completed": "[x]", "in_progress": "[>]"}
            return "\n".join(f"{mark.get(t['status'], '[ ]')} {t['content']}" for t in self.todos) or "(empty list)", False
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
            drafts = getattr(self, "_sop_drafts", [])
            self._drop_drafts([d for d in drafts if args.get("id") in (d.id, d.id.rsplit("_", 1)[0])])  # a retry
            try:
                sop, failures, extended = self.learner.extend_or_register(args, {"via": "sop_save"})
            except Exception as e:
                return f"ERROR: {e}", True
            real_failures = bool(failures and failures != ["no tests provided"])
            if real_failures and not extended:
                drafts.append(sop)
            elif sop.status == "validated":
                self._drop_drafts(list(drafts))           # it passed: the failed attempts before it are not SOPs
            active[sop.tool_name] = sop
            msg = (f"extended the existing {sop.id} (now version {sop.version}) instead of adding a near-duplicate; "
                   f"callable as {sop.tool_name}" if extended else
                   f"saved {sop.id} as {sop.status}; callable as {sop.tool_name}")
            return msg + (f"\ntest failures: {failures}" if failures else ""), real_failures
        if name.startswith("sop_"):
            return f"ERROR: {name} is not loaded; use sop_search first", True
        return self._with_hooks(name, args)

    def _drop_drafts(self, drafts: list[SOP]) -> None:
        """Remove failed sop_save attempts of this run; never a validated SOP or one saved by another run."""
        removed = False
        for d in drafts:
            if d.status != "validated" and (d.origin or {}).get("via") == "sop_save" and d.path.exists():
                shutil.rmtree(d.path, ignore_errors=True)
                removed = True
            if d in getattr(self, "_sop_drafts", []):
                self._sop_drafts.remove(d)
        if removed:
            self.lib.reload()

    def _measure_sop_use(self, sop: SOP, turn: int, turn_cost: dict | None, calls_in_turn: int,
                         exec_seconds: float) -> dict | None:
        """What one SOP call actually saved, measured.

        Without it the model would generate the procedure itself (its baseline: the measured cost of the
        repetitions it was learned from, else the size of its code), then run it. With it the model identifies
        the SOP and writes the call (its share of this turn's output) and the SOP runs. Execution happens either
        way, so the time saved is the baseline's generation time less this turn's share."""
        if not turn_cost:
            return None
        share = max(1, calls_in_turn)
        call_tokens = turn_cost["tokens"] / share
        call_gen_s = turn_cost["seconds"] / share
        tps = (turn_cost["tokens"] / turn_cost["seconds"]) if turn_cost["seconds"] > 0 and turn_cost["tokens"] else 100.0
        recorded = (sop.origin or {}).get("saves_per_use") or {}
        if recorded.get("cost_tokens"):
            base_tokens, base_gen_s, source = recorded["cost_tokens"], recorded.get("cost_seconds") or \
                recorded["cost_tokens"] / tps, "measured"
        else:
            code = "".join(f.read_text(errors="replace") for f in sop.path.glob("run.*") if f.is_file())
            base_tokens, source = len(code) / 4, "code size"
            base_gen_s = base_tokens / tps
        use = {"sop": sop.id, "turn": turn, "baseline": source,
               "baseline_tokens": round(base_tokens), "call_tokens": round(call_tokens),
               "tokens_saved": round(base_tokens - call_tokens),
               "baseline_seconds": round(base_gen_s + exec_seconds, 2),
               "call_seconds": round(call_gen_s + exec_seconds, 2), "exec_seconds": round(exec_seconds, 2),
               "seconds_saved": round(base_gen_s - call_gen_s, 2)}
        self.event("sop_use", **use)
        self.lib.record_savings(sop.id, use["tokens_saved"], use["seconds_saved"])
        return use

    def _with_hooks(self, name: str, args: dict) -> tuple[str, bool]:
        """A built-in tool call plus the lifecycle hooks on what it produced (after_output)."""
        from .tools import SKIP_DIRS
        schema = next((t for t in BUILTIN_SCHEMAS if t["name"] == name), None)
        missing = [k for k in ((schema or {}).get("input_schema") or {}).get("required", []) if k not in (args or {})]
        if missing:
            # a reply cut off at the output limit leaves a tool call with half its arguments: say so, don't crash
            return (f"ERROR: {name} is missing required argument(s): {', '.join(missing)}. If your reply was cut "
                    "off, send the complete call again (for a large file, write it in smaller parts)."), True
        local = self.env is None or getattr(self.env, "kind", "local") == "local"
        watch_bash = name == "bash" and local and self.hooks.active("after_output")
        before = hooks_mod.snapshot(self.toolbox.cwd, SKIP_DIRS) if watch_bash else None
        guard = (name in ("edit_file", "edit_lines") and local and self.cfg.get("edit_guard", True)
                 and self.hooks.active("after_output"))
        if guard:                              # was the file clean before this edit?
            target = self.toolbox._p(args["path"])
            try:
                original = target.read_text(errors="replace")
            except OSError:
                guard = False
            else:
                guard = not self.hooks.fire("after_output", paths=[str(target)])
        out, err = self.toolbox.call(name, args)
        rep = ""
        if name in ("write_file", "edit_file", "edit_lines") and not err \
                and not out.startswith(("ERROR", "DENIED")) and self.hooks.active("after_output"):
            rep = self.hooks.fire("after_output", paths=[str(self.toolbox._p(args["path"]))])
            if guard and rep:
                # an edit that breaks a working file is undone (SWE-agent's edit guard): the model retries from a
                # file that still works instead of building on a broken one
                target.write_text(original)
                new = str(args.get("new", ""))  # edit_file / edit_lines replacement text
                self.event("edit_guard", path=str(args["path"]), report=rep[:1000])
                return ("ERROR: edit rejected and undone: the file was fine before this edit, and the edit would "
                        "break it. The file is unchanged.\n" + rep + "\nThe replacement text you sent was:\n"
                        + "\n".join(new.splitlines()[:25]) + ("\n..." if new.count("\n") >= 25 else "")
                        + "\nFix the problem and send the edit again."), True
        elif before is not None:
            paths = hooks_mod.changed(before, hooks_mod.snapshot(self.toolbox.cwd, SKIP_DIRS))
            if paths:
                rep = self.hooks.fire("after_output", paths=paths)
        return (out + "\n" + rep if rep else out), err
