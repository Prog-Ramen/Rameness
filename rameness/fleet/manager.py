"""The manager: JEV-driven orchestration of a hierarchy of agents.

Every judgement call is a JEV decision recorded against the agent it concerns:

  intake      handle a director request: delegate | decompose | research | clarify
  role        each subtask: associate (does it) | lead (manages its own subtree)
  fork        is the approach uncertain enough to run alternatives in parallel?
  environment where the agent's tools run (local / ssh / container ...), by capability
  slot        which model or CLI agent runs it, by traits + track record
  reuse       hand follow-up work to an existing idle agent vs spawn a new one
  failure     retry | stronger model | fork | escalate | abandon
  stall       nudge | wait | restart | escalate
  question    an agent asks something: manager answers from context | escalate to director
  fork pick   which fork's result wins

Hard constraints (capacity, GPU requirements, privacy, permissions, merge policy)
are deterministic and never overridden by a probability.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

from .. import config as config_mod
from .. import jev as jev_mod
from ..jev import Option
from ..router import decisive
from . import envs as envs_mod
from . import sessions, worktree
from .slots import Slot, discover, provider_for
from .store import ACTIVE, Store

FLEET_DEFAULTS = {
    "backend": "auto",                 # auto | herdr | tmux | screen | subprocess
    "mode": "review",                  # review (director approves merges) | local (merge locally) | pr
    "autonomous": False,               # with mode=review: merge without asking
    "manager_slot": None,              # slot id for planning; default: strongest available model
    "privacy": "any",                  # any | local-only (only local model slots)
    "environments": [],
    "slots": [],
    "scan_ports": [],
    "discover": True,                  # probe local ports / PATH / API keys for slots
    "cli_capacity": 2,
    "max_active": 8,
    "max_depth": 3,
    "max_attempts": 2,
    "stall_seconds": 900,
    "fork_threshold": 0.62,
    "fork_width": 2,
    "worker_permissions": "auto",      # permission mode inside worker harnesses (they're sandboxed to a worktree)
    "port": 7788,
    # comfort gate: before any decision takes effect, JEV asks itself whether it is the director's to make
    "gate": {"significance": 0.55, "margin_floor": 0.06, "uncertain_significance": 0.3},
    # per-question override: "auto" (gate decides) | "jev" (never ask) | "director" (always ask); "*" = default
    "decision_policy": {"*": "auto"},
    # autonomy: restrictive (work + model-originated decisions go to the director via the manager) |
    #           balanced (comfort gate) | autopilot (JEV decides everything, bounded) |
    #           godmode (autopilot + open-ended cycles; needs allow_godmode)
    "autonomy": "balanced",
    "allow_godmode": False,
    "godmode": {"max_cycles": None},
    "max_cycles": 50,                  # cap for a requested cycle count
    "cycle_max_options": 3,            # improvements implemented per cycle at most
    "autopilot_extra_cycles": 3,       # beyond a requested count, how many cycles JEV may add on its own
}

AUTONOMY = ("restrictive", "balanced", "autopilot", "godmode")

# decision classes: infra = mechanics (where/what runs), work = shaping the work, llm = choosing among
# things a model proposed or asked. restrictive mode sends work + llm decisions to the director.
QUESTION_CLASS = {
    "How should the manager handle this request?": "work",
    "Is this a follow-up for an agent that just finished related work?": "work",
    "Should one agent do this, or should a lead manage sub-agents for it?": "work",
    "Is the best approach uncertain enough to try alternatives in parallel?": "work",
    "Which environment should this agent's tools run in?": "infra",
    "Which model or agent runtime should run this task?": "infra",
    "An agent failed. What should the manager do?": "work",
    "This agent has produced no output for a while. What now?": "infra",
    "Can the manager answer this agent's question, or must the director decide?": "llm",
    "Which parallel attempt produced the best result (tests pass, complete, simplest)?": "llm",
    "Which kind of refinement should the next cycle focus on?": "work",
    "Which proposed improvements should this cycle implement?": "llm",
    "Which test findings should this cycle fix?": "llm",
    "Are these the right kinds of tests, and do they cover the edge cases?": "llm",
    "The requested cycles are done. Is further work of this kind needed?": "work",
    "Has this work converged, or is another refinement cycle worth it?": "work",
    "Should this finished work be merged?": "work",
    "Which category of work is this task?": "label",
}

INTAKE = [
    Option("delegate", "fix add implement change update write rename bug feature file function test single focused task"),
    Option("decompose", "build system project app platform multiple several parts components and also then plus "
                        "end to end migrate across modules frontend backend pipeline full"),
    Option("research", "investigate research find out compare evaluate analyze why explore report survey options "
                       "audit review understand benchmark"),
    Option("clarify", "something stuff thing it that help improve better do make"),
]
ROLE = [
    Option("associate", "small focused single file function fix test one step quick"),
    Option("lead", "large multi part subsystem several components service feature set module migration project"),
]
FAILURE = [
    Option("retry", "timeout flaky network transient rate limit connection interrupted killed lost"),
    Option("stronger", "wrong incorrect confused could not failed tests reasoning complex hard gave up max turns loop"),
    Option("fork", "stuck approach alternative different strategy dead end"),
    Option("escalate", "permission denied credentials access approval unclear requirement ambiguous decision"),
    Option("abandon", "impossible not applicable duplicate obsolete cancelled"),
]
STALL = [
    Option("nudge", "waiting idle prompt input quiet no output"),
    Option("wait", "building compiling downloading installing training long running tests progress"),
    Option("restart", "hung frozen stuck deadlock crashed"),
    Option("escalate", "blocked needs approval credentials"),
]
QUESTION = [
    Option("answer", "which file where how default convention naming format technical detail library version "
                     "path command test framework style"),
    Option("escalate", "approve permission cost budget delete production deploy credentials secret merge publish "
                       "priority preference business product decision scope pay legal"),
]

DECOMPOSE_SYSTEM = """You are the manager of a team of AI agents. Break the request into the smallest set of
independent, delegable subtasks (1-6). Each subtask's "task" must be self-contained: context, concrete goal,
acceptance criteria. Output JSON only."""


def load_fleet_cfg(cwd: Path) -> dict:
    cfg = json.loads(json.dumps(FLEET_DEFAULTS))
    for p in (config_mod.user_home() / "fleet.json", cwd / ".rameness" / "fleet.json"):
        if p.exists():
            config_mod._merge(cfg, json.loads(p.read_text()))
    return cfg


class Fleet:
    def __init__(self, cwd: Path | None = None, fleet_cfg: dict | None = None, planner=None, out=None,
                 probe: bool = True):
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.state = self.cwd / ".rameness"
        self.cfg = fleet_cfg or load_fleet_cfg(self.cwd)
        self.base_cfg = config_mod.load(self.cwd)
        self.out = out or (lambda s: print(s, file=sys.stderr))
        self.store = Store(self.state / "fleet.db")
        self.lock = threading.RLock()
        self._inflight: set[str] = set()
        self.backend = sessions.get(self.cfg["backend"], self.state)
        self.envs: dict[str, envs_mod.Environment] = {"local": envs_mod.local(str(self.cwd))}
        for d in self.cfg["environments"]:
            e = envs_mod.from_config(d)
            self.envs[e.id] = e
        self.slots: list[Slot] = []
        self.refresh_slots()
        if probe:
            self.probe_envs()
        self.planner = (planner if planner is not None else self._planner()) or None
        self.jev = jev_mod.build(self.base_cfg, self.planner, self.state / "decisions.jsonl")
        self.store.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.manager = self._ensure_manager()
        from .cycles import Programs
        self.programs = Programs(self)

    # ------------------------------------------------------------------ autonomy

    @property
    def autonomy(self) -> str:
        r = self.store.db.execute("SELECT value FROM settings WHERE key='autonomy'").fetchone()
        return r["value"] if r else self.cfg["autonomy"]

    def set_autonomy(self, mode: str) -> str:
        if mode not in AUTONOMY:
            raise ValueError(f"autonomy must be one of {AUTONOMY}")
        if mode == "godmode" and not self.cfg["allow_godmode"]:
            raise ValueError("godmode is disabled: set \"allow_godmode\": true in fleet.json to enable it")
        self.store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('autonomy', ?)", (mode,))
        self.store.event("manager", "autonomy", mode)
        return mode

    # ------------------------------------------------------------------ inventory

    def refresh_slots(self) -> None:
        self.slots = discover(self.cfg)
        if self.cfg["privacy"] == "local-only":
            self.slots = [s for s in self.slots if s.local]

    def probe_envs(self) -> None:
        for e in self.envs.values():
            e.probe()

    def slot(self, sid: str | None) -> Slot | None:
        return next((s for s in self.slots if s.id == sid), None)

    def _planner(self):
        pref = self.cfg["manager_slot"]
        if pref == "none":
            return None                       # no planning model: single-task delegation, lexical JEV
        order = [s for s in self.slots if not s.is_cli and s.available]
        if pref:
            order = [s for s in order if s.id == pref] or order
        else:
            rank = {"anthropic": 0, "openai": 1}
            order.sort(key=lambda s: (rank.get(s.kind, 2), "haiku" in s.model))
        for s in order:
            try:
                p = provider_for(s)
                p.slot_id = s.id
                return p
            except Exception:
                continue
        return None

    def usage(self) -> tuple[dict, dict]:
        by_slot: dict[str, int] = {}
        by_env: dict[str, int] = {}
        for a in self.store.agents(status=("starting", "running", "blocked")):
            if a["runtime"] == "manager":          # the manager and leads plan in-process; they hold no slot
                continue
            by_slot[a["slot"]] = by_slot.get(a["slot"], 0) + 1
            by_env[a["env"]] = by_env.get(a["env"], 0) + 1
        return by_slot, by_env

    # ------------------------------------------------------------------ decisions

    def _gate(self, question: str, query: str, d, n_options: int, answered_free_text: bool,
              ask_director: bool = False) -> tuple[bool, float | None, str]:
        """Who makes this decision? Returns (needs_director, significance, reason)."""
        pol = self.cfg["decision_policy"]
        if answered_free_text:
            return False, None, "director answered in free text"
        if ask_director and self.autonomy not in ("autopilot", "godmode"):
            return True, None, "changes the scope you asked for"
        if question in pol and pol[question] != "auto":
            return pol[question] == "director", None, f"policy: {pol[question]}"
        cls = QUESTION_CLASS.get(question, "work")
        mode = self.autonomy
        if cls == "label" or n_options < 2:
            return False, None, "no real choice" if n_options < 2 else "labeling"
        g = self.cfg["gate"]
        if mode in ("autopilot", "godmode"):
            c = self.jev.comfort(question, query, d, g["significance"], g["margin_floor"], g["uncertain_significance"])
            return False, c.significance, f"{mode}" + (f" (would have asked: {c.reason})" if c.needs_user else "")
        if mode == "restrictive" and cls in ("work", "llm"):
            return True, None, "restrictive: the director decides"
        if pol.get("*", "auto") != "auto":
            return pol["*"] == "director", None, f"policy: {pol['*']}"
        c = self.jev.comfort(question, query, d, g["significance"], g["margin_floor"], g["uncertain_significance"])
        return c.needs_user, c.significance, c.reason

    def decide(self, agent: str | None, question: str, query: str, options: list[Option],
               default: str | None = None, context: str = "", defer: bool = True,
               payload: dict | None = None, ask_director: bool = False) -> tuple[str | None, dict]:
        """Every fleet decision goes through here.

        1. If the director already ruled on this exact decision, use that.
        2. JEV scores the options.
        3. Gate (depends on autonomy mode and the decision's class): JEV decides, or the decision
           becomes a director escalation with JEV's recommendation and this returns ``None``
           until it is answered.
        Every step is recorded for the shadow view.
        """
        ids = [o.id for o in options]
        dkey = f"{agent}|{question}|{hashlib.sha1(query.encode()).hexdigest()[:12]}"
        prior = self.store.escalation_by_key(dkey)
        if prior and prior["status"] == "open":
            return None, prior["payload"].get("probs", {})
        if prior and prior["status"] == "answered" and prior["answer"] in ids:
            return prior["answer"], prior["payload"].get("probs", {})
        d = self.jev.choose(question, query, options, context)
        chosen = decisive(d, default) if default else d.best
        needs, sig, reason = self._gate(question, query, d, len(ids), bool(prior), ask_director)
        return self._record(agent, question, query, d, chosen, needs and defer, needs, sig, reason, dkey,
                            [k for k, _ in d.top(len(ids))], payload), d.probs

    def decide_many(self, agent: str | None, question: str, query: str, options: list[Option],
                    max_k: int = 3, threshold: float = 0.45) -> list[str] | None:
        """Multi-select decision (e.g. which proposed improvements to implement). ``None`` = waiting."""
        ids = [o.id for o in options]
        dkey = f"{agent}|{question}|{hashlib.sha1((query + ','.join(o.text for o in options)).encode()).hexdigest()[:12]}"
        prior = self.store.escalation_by_key(dkey)
        if prior and prior["status"] == "open":
            return None
        if prior and prior["status"] == "answered":
            got = [x.strip() for x in prior["answer"].split(",") if x.strip() in ids]
            if got:
                return got
        d = self.jev.activate(question, query, options)
        chosen = [i for i, p in d.top(max_k) if p >= threshold] or [d.best]
        needs, sig, reason = self._gate(question, query, d, len(ids), bool(prior))
        r = self._record(agent, question, query, d, ",".join(chosen), needs, needs, sig, reason, dkey,
                         [k for k, _ in d.top(len(ids))], {"multi": True})
        return None if r is None else chosen

    def _record(self, agent, question, query, d, chosen, defer, needs, sig, reason, dkey, ordered, payload):
        gate = "deferred" if defer else ("jev-noted" if needs or "would have asked" in reason else "jev")
        self.store.decision(d.id, agent, question, d.probs, chosen, d.backend, query, gate, sig, reason)
        if defer:
            a = self.store.agent(agent) if agent else None
            who = f"for {a['title']}" if a and a["role"] != "manager" else ""
            self.store.escalate(agent, f"Manager: {question} {who}\n\n{query[:900]}\n\nJEV leans '{chosen}' - "
                                       f"deferred to you ({reason}).",
                                ordered, kind="decision", dkey=dkey,
                                payload={"decision": d.id, "probs": d.probs, "recommended": chosen, **(payload or {})})
            return None
        return chosen

    # ------------------------------------------------------------------ CRUD

    def _ensure_manager(self) -> dict:
        m = self.store.agents(parent=None)
        m = [a for a in m if a["role"] == "manager"]
        if m:
            return m[0]
        return self.store.create_agent(id="manager", role="manager", kind="manage", title="Manager",
                                       task="coordinate the director's requests", status="running",
                                       runtime="manager", slot=getattr(self.planner, "slot_id", None), env="local")

    def spawn(self, task: str, parent: str = "manager", role: str = "associate", kind: str = "deliver",
              title: str | None = None, slot: str | None = None, env: str | None = None,
              depends: list[str] | None = None, meta: dict | None = None, role_explicit: bool = True) -> dict:
        """Create an agent (queued). Unless ``role_explicit``, JEV decides associate-vs-lead
        (and whether to fork) when the agent is scheduled - so those decisions can wait on the director."""
        with self.lock:
            p = self.store.agent(parent)
            if p is None:
                raise KeyError(f"no agent {parent}")
            depth = int(p["meta"].get("depth", 0)) + 1
            if depth > self.cfg["max_depth"] and role == "lead":
                role = "associate"
            meta = {"depth": depth, "pinned_slot": bool(slot), "pinned_env": bool(env), **(meta or {})}
            meta.setdefault("role_decided", role_explicit)
            a = self.store.create_agent(parent=parent, role=role, kind=kind, title=title or task[:60], task=task,
                                        slot=slot, env=env, depends=depends or [], meta=meta)
            return a

    def get(self, aid: str) -> dict:
        a = self.store.agent(aid)
        if a is None:
            raise KeyError(f"no agent {aid}")
        return a

    def tree(self, root: str = "manager") -> dict:
        a = self.get(root)
        return {**self._public(a), "children": [self.tree(c["id"]) for c in self.store.children(root)]}

    def _public(self, a: dict) -> dict:
        h = a.get("handle") or {}
        out = {k: a[k] for k in ("id", "parent", "role", "kind", "title", "status", "runtime", "slot", "env",
                                 "branch", "worktree", "depends", "forked_from", "attempts", "created", "updated")}
        out["meta"] = a["meta"]
        out["attach"] = self.backend_for(h).attach_cmd(h) if h else None
        out["result"] = (a.get("result") or "")[:4000]
        out["task"] = a["task"]
        return out

    def backend_for(self, h: dict) -> sessions.Backend:
        if not h or h.get("backend") == self.backend.name:
            return self.backend
        return sessions.BACKENDS[h["backend"]](self.state)

    def prompt(self, aid: str, text: str, sender: str = "director") -> str:
        """Send an instruction to an agent: live injection, TTY input, or reopen a finished agent."""
        with self.lock:
            a = self.get(aid)
            if a["status"] in ("running", "blocked", "starting", "queued", "paused") and a["runtime"] == "rameness":
                self.store.send(aid, text, sender)
                return "queued for the agent's next turn"
            if a["status"] in ("running", "blocked") and a["handle"]:
                ok = self.backend_for(a["handle"]).send(a["handle"], text)
                self.store.event(aid, "message", f"director (tty): {text[:200]}")
                return "sent to session" if ok else "this session backend can't accept input"
            if a["status"] in ("done", "failed", "retired"):
                self.store.send(aid, text, sender)
                self.store.update_agent(aid, meta={**a["meta"], "resume": True, "finalized": False,
                                                   "integrated": False})
                self.store.set_status(aid, "queued", "reopened with a follow-up")
                return "reopened"
            return f"agent is {a['status']}"

    def reassign(self, aid: str, slot: str | None = None, env: str | None = None) -> dict:
        with self.lock:
            a = self.get(aid)
            if slot and not self.slot(slot):
                raise KeyError(f"no slot {slot}")
            if env and env not in self.envs:
                raise KeyError(f"no environment {env}")
            restart = a["status"] in ("running", "blocked", "starting")
            if restart:
                self._stop_session(a)
            meta = {**a["meta"], "pinned_slot": bool(slot) or a["meta"].get("pinned_slot"),
                    "pinned_env": bool(env) or a["meta"].get("pinned_env"), "resume": restart}
            self.store.update_agent(aid, slot=slot or a["slot"], env=env or a["env"], meta=meta)
            if restart:
                self.store.set_status(aid, "queued", f"reassigned to {slot or a['slot']} @ {env or a['env']}")
            return self.get(aid)

    def pause(self, aid: str) -> None:
        with self.lock:
            for a in [self.get(aid), *self.store.subtree(aid)]:
                if a["status"] in ("running", "blocked", "starting", "queued"):
                    self._stop_session(a)
                    self.store.update_agent(a["id"], meta={**a["meta"], "resume": True})
                    self.store.set_status(a["id"], "paused")

    def resume(self, aid: str) -> None:
        with self.lock:
            for a in [self.get(aid), *self.store.subtree(aid)]:
                if a["status"] == "paused":
                    self.store.set_status(a["id"], "running" if a["role"] == "lead" and a["runtime"] == "manager"
                                          else "queued", "resumed")

    def retire(self, aid: str, delete: bool = False, drop_branch: bool = False) -> None:
        with self.lock:
            for a in [*reversed(self.store.subtree(aid)), self.get(aid)]:
                if a["id"] == "manager":
                    continue
                self._stop_session(a)
                if a["worktree"]:
                    try:
                        worktree.remove(self.envs.get(a["env"] or "local", self.envs["local"]), a["worktree"],
                                        a["branch"], delete_branch=drop_branch)
                    except Exception as e:
                        self.store.event(a["id"], "warning", f"worktree cleanup: {e}")
                if delete:
                    self.store.delete_agent(a["id"])
                elif a["status"] not in ("done", "failed"):
                    self.store.set_status(a["id"], "retired")

    def fork(self, aid: str, instructions: list[str] | None = None, n: int | None = None) -> list[dict]:
        """Clone an agent's task (and transcript, if it has one) into parallel variants."""
        with self.lock:
            a = self.get(aid)
            n = n or len(instructions or []) or self.cfg["fork_width"]
            instructions = instructions or self._variants(a["task"], n)
            group = a["meta"].get("fork_group") or f"fg-{a['id']}"
            if not a["meta"].get("fork_group"):
                self.store.update_agent(aid, meta={**a["meta"], "fork_group": group})
            out = []
            for i, ins in enumerate(instructions[:n]):
                f = self.spawn(f"{a['task']}\n\nApproach for this attempt: {ins}", parent=a["parent"], role="associate",
                               kind=a["kind"], title=f"{a['title'][:40]} [fork {i + 1}]", depends=a["depends"],
                               meta={"fork_group": group, "fork_of": aid, "variant": ins,
                                     "transcript_from": aid if a["runtime"] == "rameness" else None})
                self.store.update_agent(f["id"], forked_from=aid)
                out.append(f)
            self.store.event(aid, "fork", f"forked into {[f['id'] for f in out]}")
            return out

    def _variants(self, task: str, n: int) -> list[str]:
        if self.planner:
            try:
                d = self.planner.complete_json("Propose genuinely different approaches. JSON only.",
                                               f"Task: {task}\nGive {n} distinct approaches as "
                                               '{"approaches": ["one sentence each", ...]}', max_tokens=800)
                v = [x for x in d.get("approaches", []) if isinstance(x, str)]
                if len(v) >= n:
                    return v[:n]
            except Exception:
                pass
        base = ["prioritise the simplest correct solution", "prioritise performance and robustness",
                "take an unconventional approach", "reuse existing libraries wherever possible"]
        return base[:n]

    def answer(self, eid: str, text: str) -> None:
        with self.lock:
            e = self.store.escalation(eid)
            if not e:
                raise KeyError(eid)
            self.store.answer(eid, text)
            if e["kind"] == "decision":
                pl = e["payload"]
                if pl.get("multi"):
                    self.store.set_gate(pl.get("decision", ""), "director", text)
                elif text in e["options"]:
                    from ..improve import Tuner
                    Tuner(self.state).feedback(pl.get("decision", ""), label=text)
                    self.store.set_gate(pl.get("decision", ""), "director", text)
                    self.store.decision_outcome(pl.get("decision", ""), f"label:{text}")
                elif e["agent"] and e["agent"] != "manager":
                    self.prompt(e["agent"], text)
                if pl.get("resume") == "ask":
                    self.ask(pl["text"], clarified=pl.get("clarified", False))
            elif e["kind"] == "merge" and e["agent"]:
                self._apply_merge_answer(self.get(e["agent"]), text)
            elif e["kind"] == "intake":
                self.ask(f"{e['options'][0] if e['options'] else ''}\n\nClarification: {text}".strip(), clarified=True)
            elif e["kind"] == "failure" and e["agent"]:
                a = self.get(e["agent"])
                if text.lower().startswith(("retry", "yes", "y")):
                    self.store.set_status(a["id"], "queued", "director: retry")
                elif text.lower().startswith(("abandon", "no", "n")):
                    self.store.set_status(a["id"], "retired", "director: abandon")
                else:
                    self.prompt(a["id"], text)

    # ------------------------------------------------------------------ intake

    def ask(self, text: str, clarified: bool = False, cycles: int | None | str = 0,
            categories: list[str] | None = None, on: str | None = None) -> dict:
        """Director request -> JEV intake route -> agents.

        ``cycles``: N > 0 builds a draft then runs N refinement cycles; ``"godmode"`` cycles until JEV
        judges the work converged (godmode autonomy only)."""
        if cycles:
            with self.lock:
                p = self.programs.create(text, None if cycles == "godmode" else int(cycles), categories,
                                         draft=on is None, from_agent=None if on in (None, "HEAD") else on)
                return {"route": "program", "program": p["id"], "agents": [p["lead"]], "probs": {}}
        with self.lock:
            m = self.manager["id"]
            resume = {"resume": "ask", "text": text, "clarified": clarified}
            route, probs = self.decide(m, "How should the manager handle this request?", text, INTAKE, "delegate",
                                       payload=resume)
            if route is None:
                return {"route": "awaiting-director", "probs": probs}
            self.store.event(m, "intake", f"{route}: {text[:200]}")
            if route == "clarify" and not clarified:
                eid = self.store.escalate(m, f"Please clarify the request: {text}", [text], kind="intake")
                return {"route": route, "escalation": eid, "probs": probs}
            if route == "decompose" and self.planner:
                return {"route": route, "probs": probs, "agents": [a["id"] for a in self.decompose(m, text)]}
            kind = "research" if route == "research" else "deliver"
            reuse = self._reuse(text, resume)
            if reuse == "?":
                return {"route": "awaiting-director", "probs": probs}
            if reuse:
                self.prompt(reuse, text)
                return {"route": "reuse", "probs": probs, "agents": [reuse]}
            a = self.spawn(text, parent=m, kind=kind, role_explicit=False, meta={"size": "small"})
            return {"route": route, "probs": probs, "agents": [a["id"]]}

    def _reuse(self, text: str, payload: dict | None = None) -> str | None:
        idle = [a for a in self.store.agents(status=("done",)) if a["role"] == "associate"
                and a["runtime"] == "rameness" and time.time() - a["updated"] < 3600]
        if not idle:
            return None
        opts = [Option(a["id"], f"{a['title']} {a['task'][:300]}") for a in idle[-8:]] + [
            Option("new", "unrelated new different separate fresh task")]
        chosen, _ = self.decide(self.manager["id"], "Is this a follow-up for an agent that just finished related work?",
                                text, opts, "new", payload=payload)
        if chosen is None:
            return "?"
        return None if chosen == "new" else chosen

    def _role(self, a: dict) -> str | None:
        prior = {"large": (0.6, 1.5), "small": (1.4, 0.6)}.get(a["meta"].get("size", ""), (1, 1))
        opts = [Option(o.id, o.text, p) for o, p in zip(ROLE, prior)]
        chosen, _ = self.decide(a["id"], "Should one agent do this, or should a lead manage sub-agents for it?",
                                a["task"], opts, "associate")
        return chosen

    def decompose(self, parent: str, text: str) -> list[dict]:
        try:
            plan = self.planner.complete_json(DECOMPOSE_SYSTEM, (
                f"Request: {text}\n\nReply {{\"subtasks\": [{{\"title\": str, \"task\": str, "
                '"kind": "deliver"|"research", "depends_on": [indices], "size": "small"|"large", '
                '"needs": ["gpu"|"network"|...]}]}'), max_tokens=6000)
            subs = plan.get("subtasks") or []
        except Exception as e:
            self.store.event(parent, "warning", f"decomposition failed ({e}); delegating whole")
            subs = []
        if not subs:
            subs = [{"title": text[:60], "task": text, "kind": "deliver", "size": "small"}]
        created: list[dict] = []
        for s in subs[:6]:
            deps = [created[i]["id"] for i in s.get("depends_on", []) if isinstance(i, int) and i < len(created)]
            a = self.spawn(s["task"], parent=parent, kind=s.get("kind", "deliver"), title=s.get("title"),
                           depends=deps, role_explicit=False,
                           meta={"needs": s.get("needs", []), "size": s.get("size", "small")})
            created.append(a)
        self.store.event(parent, "plan", " | ".join(f"{a['id']}:{a['title']}" for a in created))
        return created

    def _maybe_fork(self, a: dict) -> list[dict] | None:
        chosen, probs = self.decide(a["id"], "Is the best approach uncertain enough to try alternatives in parallel?",
                                    a["task"], [Option("fork", "optimize performance speed faster experiment try "
                                                       "approaches alternatives compare best uncertain tune algorithm "
                                                       "design explore prototype benchmark"),
                                                Option("single", "simple straightforward fix rename add update "
                                                       "documented small typo config write test")], "single")
        if chosen is None:
            return None
        if (chosen != "fork" or probs.get("fork", 0) < self.cfg["fork_threshold"]
                or len(self.store.agents(status=ACTIVE)) + 2 > self.cfg["max_active"] * 2):
            return []
        return self.fork(a["id"], n=self.cfg["fork_width"] - 1)

    # ------------------------------------------------------------------ assignment

    def assign(self, a: dict) -> bool:
        by_slot, by_env = self.usage()
        needs = a["meta"].get("needs", [])
        # environment
        if not a["meta"].get("pinned_env") or not a["env"]:
            cands = [e for e in self.envs.values() if e.info.get("reachable", e.kind == "local")
                     and by_env.get(e.id, 0) < e.max_agents and ("gpu" not in needs or e.has_gpu)]
            if not cands:
                return False
            opts = [Option(e.id, e.text, self.store.rate(f"env:{e.id}") * 2 * (1.15 if e.kind == "local" else 1))
                    for e in cands]
            env, _ = self.decide(a["id"], "Which environment should this agent's tools run in?",
                                 f"{a['task']} {' '.join(needs)}", opts, cands[0].id if len(cands) == 1 else "local"
                                 if "local" in [e.id for e in cands] else cands[0].id)
            if env is None:
                return False
        else:
            env = a["env"]
        # model slot / runtime
        if a["role"] == "lead":
            self.store.update_agent(a["id"], env=env, runtime="manager", slot=getattr(self.planner, "slot_id", None))
            return True
        if not a["meta"].get("pinned_slot") or not a["slot"]:
            cands = [s for s in self.slots if s.available and by_slot.get(s.id, 0) + s.busy_server < s.capacity
                     and not (s.is_cli and self.envs[env].kind != "local" and s.model not in self.envs[env].tags)
                     and not (s.is_cli and a["meta"].get("transcript_from"))
                     and s.id not in a["meta"].get("exclude_slots", [])]
            if not cands:
                return False
            opts = [Option(s.id, s.text, self.store.rate(f"slot:{s.id}") * 2) for s in cands]
            slot, _ = self.decide(a["id"], "Which model or agent runtime should run this task?", a["task"], opts,
                                  cands[0].id)
            if slot is None:
                return False
        else:
            slot = a["slot"]
        s = self.slot(slot)
        if s is None:
            return False
        self.store.update_agent(a["id"], env=env, slot=slot, runtime=("cli:" + s.model) if s.is_cli else "rameness")
        return True

    # ------------------------------------------------------------------ lifecycle

    def start(self, a: dict) -> None:
        env = self.envs.get(a["env"] or "local", self.envs["local"])
        self.store.set_status(a["id"], "starting")
        if a["role"] == "lead":
            self._start_lead(a, env)
            return
        workdir = a["worktree"]
        if not workdir:
            workdir = env.workdir if env.kind != "local" else str(self.cwd)
            base = a["meta"].get("base_branch")
            if (a["kind"] == "deliver" or base) and worktree.is_repo(env, workdir):
                parent = self.store.agent(a["parent"]) or {}
                try:
                    workdir, branch = worktree.create(env, workdir, a["id"], base or parent.get("branch"))
                    self.store.update_agent(a["id"], worktree=workdir, branch=branch)
                except RuntimeError as e:
                    self.store.event(a["id"], "warning", f"no worktree: {e}")
        if a["runtime"] == "rameness":
            argv = [sys.executable, "-m", "rameness.fleet.worker", "--root", str(self.cwd), "--id", a["id"]]
            local_cwd = str(self.cwd)
        else:
            s = self.slot(a["slot"])
            brief = self._brief(a)                   # once: it consumes pending follow-ups
            argv = [x.replace("{task}", brief) for x in s.argv]
            if env.kind != "local":
                argv = env.interactive_argv(argv, workdir)
                local_cwd = str(self.cwd)
            else:
                local_cwd = workdir
        try:
            h = self.backend.start(a["id"], argv, local_cwd, {"RAMENESS_AGENT": a["id"], "RAMENESS_ROOT": str(self.cwd)})
        except Exception as e:
            self.store.set_status(a["id"], "failed", f"session start: {e}")
            return
        self.store.update_agent(a["id"], handle=h, backend=h["backend"], attempts=a["attempts"] + 1)
        self.store.set_status(a["id"], "running", f"{a['runtime']} on {a['slot']} @ {env.id} ({h['backend']})")

    def _brief(self, a: dict) -> str:
        follow = self.store.inbox(a["id"])            # CLI agents can't read the inbox: fold messages into the brief
        extra = "".join(f"\n\nFollow-up from your {m['sender']}: {m['text']}" for m in follow)
        return (f"{a['task']}{extra}\n\nYou are agent {a['id']} in a rameness team. Work only in the current directory. "
                "When finished, end with a short report: what changed, how you verified it, anything left open.")

    def _start_lead(self, a: dict, env: envs_mod.Environment) -> None:
        workdir = env.workdir if env.kind != "local" else str(self.cwd)
        if a["kind"] == "deliver" and worktree.is_repo(env, workdir):
            parent = self.store.agent(a["parent"]) or {}
            try:
                path, branch = worktree.create(env, workdir, a["id"], parent.get("branch"))
                self.store.update_agent(a["id"], worktree=path, branch=branch)
            except RuntimeError as e:
                self.store.event(a["id"], "warning", f"no worktree: {e}")
        self.store.set_status(a["id"], "running", "lead: planning its team")
        if self.planner:
            # model calls never run inside tick(): slow planners must not stall the rest of the fleet
            self._background(f"plan:{a['id']}", self.decompose, a["id"], a["task"])
        else:
            self.spawn(a["task"], parent=a["id"], kind=a["kind"], title=a["title"], role="associate")

    def _background(self, key: str, fn, *args) -> None:
        if key in self._inflight:
            return
        self._inflight.add(key)

        def run():
            try:
                fn(*args)
            except Exception as e:
                self.store.event(None, "error", f"{key}: {type(e).__name__}: {e}")
            finally:
                self._inflight.discard(key)
        threading.Thread(target=run, daemon=True, name=key).start()

    def _stop_session(self, a: dict) -> None:
        if a.get("handle"):
            try:
                self.backend_for(a["handle"]).stop(a["handle"])
            except Exception:
                pass

    # ------------------------------------------------------------------ supervision loop

    def tick(self) -> None:
        with self.lock:
            self._schedule()
            for a in self.store.agents(status=("running", "starting", "blocked")):
                self._supervise(a)
            # workers report done/failed themselves; finalize (commit, merge, feedback) what isn't yet
            for a in self.store.agents(status=("done", "failed")):
                if not a["meta"].get("finalized") and a["runtime"] != "manager":
                    self._finished(a)
            for a in self.store.agents(status=("failed",)):
                if a["meta"].get("failure_pending"):
                    self._failed(a)
            groups = {a["meta"]["fork_group"] for a in self.store.agents(status=("done",))
                      if a["meta"].get("fork_group") and not a["meta"].get("integrated") and not a["meta"].get("fork_lost")}
            for g in groups:
                self._resolve_forks(g)
            self._auto_answer()
            self.programs.tick()

    def _deps_done(self, a: dict) -> bool:
        return all((self.store.agent(d) or {}).get("status") == "done" for d in a["depends"])

    def _schedule(self) -> None:
        active = [a for a in self.store.agents(status=("starting", "running", "blocked"))
                  if not (a["role"] == "lead" and a["runtime"] == "manager")]
        room = self.cfg["max_active"] - len(active)
        for a in self.store.agents(status=("queued",)):
            parent = self.store.agent(a["parent"]) or {}
            if parent.get("status") == "paused" or not self._deps_done(a):
                continue
            if not a["meta"].get("category"):
                from .cycles import CATEGORY_TAG_Q, taxonomy_options
                cat, _ = self.decide(a["id"], CATEGORY_TAG_Q, a["task"][:600], taxonomy_options(), defer=False)
                a = self.store.update_agent(a["id"], meta={**a["meta"], "category": cat})
            if not a["meta"].get("role_decided"):
                role = self._role(a)
                if role is None:
                    continue                       # the director is deciding
                a = self.store.update_agent(a["id"], role=role, meta={**a["meta"], "role_decided": True})
            if (a["kind"] == "deliver" and a["role"] == "associate" and not a["meta"].get("fork_decided")
                    and not a["meta"].get("fork_of") and not a["meta"].get("fork_group")):
                forks = self._maybe_fork(a)
                if forks is None:
                    continue
                a = self.store.update_agent(a["id"], meta={**self.get(a["id"])["meta"], "fork_decided": True})
            if a["role"] != "lead" and room <= 0:
                break
            if not self.assign(a):
                continue
            self.start(self.get(a["id"]))
            if a["role"] != "lead":
                room -= 1

    def _supervise(self, a: dict) -> None:
        if a["role"] == "lead" and a["runtime"] == "manager":
            self._supervise_lead(a)
            return
        h = a.get("handle")
        if not h:
            return
        b = self.backend_for(h)
        state, code = b.status(h)
        fresh = self.get(a["id"])          # workers write their own status
        if fresh["status"] in ("done", "failed"):
            self._finished(fresh)
            return
        if state == "exited":
            if a["runtime"] == "rameness":      # worker died without reporting
                self.store.update_agent(a["id"], result=b.read(h, 60))
                self.store.set_status(a["id"], "failed", f"worker exited {code} without a report")
            else:
                self.store.update_agent(a["id"], result=b.read(h, 120))
                self.store.set_status(a["id"], "done" if code == 0 else "failed", f"exit {code}")
            self._finished(self.get(a["id"]))
            return
        if state == "lost":
            self.store.set_status(a["id"], "failed", "session lost")
            self._finished(self.get(a["id"]))
            return
        if isinstance(b, sessions.HerdrBackend):
            st = b.agent_status(h)
            if st == "blocked" and a["status"] != "blocked":
                self.store.set_status(a["id"], "blocked", "herdr: agent waiting for input")
                self.store.escalate(a["id"], f"{a['title']} is waiting for input:\n{b.read(h, 25)}", kind="tty")
            elif st == "working" and a["status"] == "blocked":
                self.store.set_status(a["id"], "running")
        log = Path(h.get("log", ""))
        if a["status"] == "running" and log.exists() and time.time() - log.stat().st_mtime > self.cfg["stall_seconds"]:
            if not a["meta"].get("stall_handled_at") or time.time() - a["meta"]["stall_handled_at"] > self.cfg["stall_seconds"]:
                self._stalled(a, b.read(h, 30))

    def _stalled(self, a: dict, tail: str) -> None:
        action, _ = self.decide(a["id"], "This agent has produced no output for a while. What now?",
                                f"{a['task'][:300]}\n{tail}", STALL, "wait")
        if action is None:
            return
        self.store.update_agent(a["id"], meta={**a["meta"], "stall_handled_at": time.time()})
        self.store.event(a["id"], "stall", action)
        if action == "nudge":
            self.prompt(a["id"], "Status check: continue with the task, or report if you are blocked.")
        elif action == "restart":
            self._stop_session(a)
            self.store.update_agent(a["id"], meta={**a["meta"], "resume": True, "stall_handled_at": time.time()})
            self.store.set_status(a["id"], "queued", "restarted after stall")
        elif action == "escalate":
            self.store.escalate(a["id"], f"{a['title']} appears stalled:\n{tail[-800:]}", ["wait", "restart", "retire"],
                                kind="stall")

    def _finished(self, a: dict) -> None:
        if a["meta"].get("finalized"):
            return
        ok = a["status"] == "done"
        # implicit JEV feedback: did the assignments made for this agent work out?
        from ..improve import Tuner
        tuner = Tuner(self.state)
        for d in self.store.decisions(agent=a["id"], limit=50):
            if d["question"].startswith(("Which model", "Which environment")) and not d.get("outcome"):
                self.store.decision_outcome(d["id"], "ok" if ok else "fail")
                tuner.feedback(d["id"], outcome="ok" if ok else "fail")
        self.store.bump(f"slot:{a['slot']}", ok)
        self.store.bump(f"env:{a['env']}", ok)
        env = self.envs.get(a["env"] or "local", self.envs["local"])
        if ok and a["worktree"]:
            worktree.commit_all(env, a["worktree"], f"{a['id']}: {a['title']}")
        meta = {**a["meta"], "finalized": True}
        self.store.update_agent(a["id"], meta=meta)
        a = self.get(a["id"])
        if not ok:
            self._failed(a)
            return
        if a["meta"].get("fork_group"):
            return                                  # tick() resolves the group once every attempt is finished
        self._integrate(a)

    def _failed(self, a: dict) -> None:
        if a["attempts"] >= self.cfg["max_attempts"] + 1:
            self.store.escalate(a["id"], f"{a['title']} failed after {a['attempts']} attempts:\n{(a['result'] or '')[-800:]}",
                                ["retry", "abandon", "<instructions>"], kind="failure")
            return
        action, _ = self.decide(a["id"], "An agent failed. What should the manager do?",
                                f"{a['task'][:300]}\nError: {(a['result'] or '')[-600:]}", FAILURE, "retry")
        if action is None:
            if not a["meta"].get("failure_pending"):
                self.store.update_agent(a["id"], meta={**a["meta"], "failure_pending": True})
            return
        meta = {**a["meta"], "finalized": False, "failure_pending": False, "resume": action in ("retry", "stronger")}
        if action == "retry":
            self.store.update_agent(a["id"], meta=meta)
            self.store.set_status(a["id"], "queued", "retrying")
        elif action == "stronger":
            meta.update(exclude_slots=[*a["meta"].get("exclude_slots", []), a["slot"]], pinned_slot=False)
            self.store.update_agent(a["id"], meta=meta)
            self.store.set_status(a["id"], "queued", "retrying on a different model")
        elif action == "fork":
            self.store.update_agent(a["id"], meta={**meta, "finalized": True})
            self.fork(a["id"])
        elif action == "escalate":
            self.store.update_agent(a["id"], meta={**meta, "finalized": True})
            self.store.escalate(a["id"], f"{a['title']} failed:\n{(a['result'] or '')[-800:]}",
                                ["retry", "abandon", "<instructions>"], kind="failure")
        else:
            self.store.update_agent(a["id"], meta={**meta, "finalized": True})
            self.store.set_status(a["id"], "retired", "abandoned by manager")

    def _resolve_forks(self, group: str) -> None:
        members = [x for x in self.store.agents() if x["meta"].get("fork_group") == group]
        if any(x["status"] in ACTIVE for x in members):
            return
        done = [x for x in members if x["status"] == "done"]
        if not done:
            return
        if len(done) == 1:
            winner = done[0]["id"]
        else:
            opts = [Option(x["id"], f"{x['meta'].get('variant', 'original')} :: {(x['result'] or '')[-600:]}")
                    for x in done]
            winner, _ = self.decide(done[0]["parent"], "Which parallel attempt produced the best result "
                                    "(tests pass, complete, simplest)?", done[0]["task"][:500], opts, done[0]["id"])
            if winner is None:
                return
        self.store.event(winner, "fork-winner", f"group {group}")
        for x in members:
            if x["id"] != winner and x["status"] == "done":
                self.store.update_agent(x["id"], meta={**x["meta"], "fork_lost": True})
                self.retire(x["id"])
        self._integrate(self.get(winner))

    def _integrate(self, a: dict) -> None:
        """Merge a finished deliver agent's branch into its parent's branch, or ask the director."""
        if a["meta"].get("integrated"):
            return
        self.store.update_agent(a["id"], meta={**self.get(a["id"])["meta"], "integrated": True})
        if a["kind"] != "deliver" or not a["branch"]:
            return
        if a["meta"].get("program"):
            return                                  # cycles build on each other's branches; merged once at the end
        parent = self.store.agent(a["parent"])
        env = self.envs.get(a["env"] or "local", self.envs["local"])
        if parent and parent["role"] == "lead" and parent["worktree"]:
            ok, msg = worktree.merge(env, parent["worktree"], a["branch"])
            self.store.event(a["id"], "merged" if ok else "merge-conflict", f"into {parent['branch']}: {msg[:300]}")
            if not ok:
                self.store.escalate(a["id"], f"Merge conflict integrating {a['branch']} into {parent['branch']}",
                                    ["keep branch", "discard"], kind="merge")
            return
        stat = worktree.diffstat(env, a["worktree"], "HEAD~1") if a["worktree"] else ""
        if self.autonomy in ("autopilot", "godmode"):
            verdict, _ = self.decide(a["id"], "Should this finished work be merged?",
                                     f"{a['title']} {(a['result'] or '')[-400:]}",
                                     [Option("merge", "done complete tests pass verified finished works"),
                                      Option("keep branch", "partial incomplete unverified experimental failing risky")],
                                     "keep branch")
            self._apply_merge_answer(a, verdict or "keep branch")
            self.store.event(a["id"], "merge-decision", f"JEV ({self.autonomy}): {verdict}")
        elif self.cfg["mode"] == "local" or (self.cfg["mode"] == "review" and self.cfg["autonomous"]):
            self._apply_merge_answer(a, "merge")
        else:
            self.store.escalate(a["id"], f"{a['title']} finished on {a['branch']}.\n{stat}\n\n{(a['result'] or '')[-1500:]}",
                                ["merge", "keep branch", "discard"], kind="merge")

    def _apply_merge_answer(self, a: dict, answer: str) -> None:
        env = self.envs.get(a["env"] or "local", self.envs["local"])
        repo = env.workdir if env.kind != "local" else str(self.cwd)
        ans = answer.lower()
        if ans.startswith("merge"):
            ok, msg = worktree.merge(env, repo, a["branch"])
            self.store.event(a["id"], "merged" if ok else "merge-conflict", msg[:300])
            if ok and a["worktree"]:
                worktree.remove(env, a["worktree"])
        elif ans.startswith("discard"):
            if a["worktree"]:
                worktree.remove(env, a["worktree"], a["branch"], delete_branch=True)
            self.store.event(a["id"], "discarded", a["branch"] or "")

    def _supervise_lead(self, a: dict) -> None:
        if a["meta"].get("program_lead") or f"plan:{a['id']}" in self._inflight:
            return                                  # the cycle engine owns program leads / still planning
        kids = self.store.children(a["id"])
        if not kids or any(k["status"] in ACTIVE for k in kids):
            return
        if any(k["status"] == "failed" and not k["meta"].get("fork_lost") for k in kids):
            pending = self.store.escalations()
            if any(e["agent"] in [k["id"] for k in kids] for e in pending):
                return
        report = "\n\n".join(f"## {k['title']} ({k['status']})\n{(k['result'] or '')[-1500:]}" for k in kids
                             if not k["meta"].get("fork_lost") and k["status"] != "retired")
        self.store.update_agent(a["id"], result=report)
        ok = all(k["status"] in ("done", "retired") for k in kids)
        self.store.set_status(a["id"], "done" if ok else "failed", f"team finished ({len(kids)} agents)")
        self._finished(self.get(a["id"]))

    def _auto_answer(self) -> None:
        """Agent questions: the manager answers what it can; the director gets the rest."""
        for e in self.store.escalations():
            if e["kind"] != "agent-question" or e.get("answer") or (e.get("options") and "__triaged" in e["options"]):
                continue
            a = self.store.agent(e["agent"]) or {}
            if self.autonomy == "restrictive":
                # firstmate principle: agents never reach the director directly; the manager relays
                self.store.db.execute("UPDATE escalations SET options=?, question=? WHERE id=?",
                                      (json.dumps([*e["options"], "__triaged"]),
                                       f"Manager relays from {a.get('title', e['agent'])}: {e['question']}", e["id"]))
                continue
            route, _ = self.decide(e["agent"], "Can the manager answer this agent's question, or must the director decide?",
                                   e["question"], QUESTION, "escalate", defer=False)
            if self.autonomy in ("autopilot", "godmode"):
                route = "answer"
            self.store.db.execute("UPDATE escalations SET options=? WHERE id=?",
                                  (json.dumps([*e["options"], "__triaged"]), e["id"]))
            if route == "answer" and not self.planner and self.autonomy in ("autopilot", "godmode"):
                self.store.answer(e["id"], "No one is available to decide this. Use your best judgement, "
                                           "prefer the reversible option, and state the assumption in your report.")
            elif route == "answer" and self.planner:
                self._background(f"answer:{e['id']}", self._answer_with_model, e, a)

    def _answer_with_model(self, e: dict, a: dict) -> None:
        """Runs in a background thread: the manager's model answers an agent's question from context."""
        siblings = "\n".join(f"- {s['title']}: {(s['result'] or '')[:300]}" for s in
                             self.store.children(a.get("parent") or "manager") if s["status"] == "done")
        try:
            r = self.planner.chat("You are the engineering manager answering a team member's question "
                                  "concisely from the task context. If you cannot know, say so.",
                                  [{"role": "user", "content": f"Task: {a.get('task', '')}\nFinished team "
                                    f"work:\n{siblings}\n\nQuestion: {e['question']}"}], [],
                                  effort="low", max_tokens=2000)
            if r.text and ("cannot know" not in r.text.lower() or self.autonomy in ("autopilot", "godmode")):
                self.store.answer(e["id"], r.text)
        except Exception as ex:
            self.store.event(e["agent"], "warning", f"auto-answer failed: {ex}")

    def run_forever(self, interval: float = 2.0, stop: threading.Event | None = None) -> None:
        last_slots = time.time()
        while not (stop and stop.is_set()):
            try:
                self.tick()
            except Exception as e:  # the loop must survive anything a single agent does
                self.store.event(None, "error", f"tick: {type(e).__name__}: {e}")
            if time.time() - last_slots > 30:
                self.refresh_slots()
                last_slots = time.time()
            time.sleep(interval)

    # ------------------------------------------------------------------ views

    def snapshot(self) -> dict:
        by_slot, by_env = self.usage()
        return {
            "slots": [s.summary(by_slot.get(s.id, 0)) for s in self.slots],
            "envs": [{**e.summary(), "busy": by_env.get(e.id, 0)} for e in self.envs.values()],
            "tree": self.tree(),
            "escalations": [{**e, "options": [o for o in e["options"] if o != "__triaged"]}
                            for e in self.store.escalations()],
            "decisions": self.store.decisions(limit=60),
            "gate": self.cfg["gate"], "decision_policy": self.cfg["decision_policy"],
            "autonomy": self.autonomy, "allow_godmode": self.cfg["allow_godmode"],
            "programs": self.programs.all(),
            "taxonomy": [{k: c[k] for k in ("id", "group", "label")} for c in __import__(
                "rameness.fleet.cycles", fromlist=["TAXONOMY"]).TAXONOMY],
            "events": self.store.events(limit=120),
            "config": {k: self.cfg[k] for k in ("backend", "mode", "autonomous", "privacy", "max_active", "max_depth")},
            "backend": self.backend.name,
            "planner": getattr(self.planner, "slot_id", None),
            "jev": self.jev.backend.name,
        }

    def standup(self) -> str:
        agents = self.store.agents()
        by = {}
        for a in agents:
            by.setdefault(a["status"], []).append(a)
        lines = [f"# Standup - {time.strftime('%Y-%m-%d %H:%M')}"]
        for st in ("blocked", "running", "queued", "done", "failed", "paused"):
            if by.get(st):
                lines.append(f"\n## {st} ({len(by[st])})")
                lines += [f"- {a['id']} [{a['role']}] {a['title']}  ({a['slot'] or '-'} @ {a['env'] or '-'})" for a in by[st]]
        esc = self.store.escalations()
        if esc:
            lines.append(f"\n## needs your decision ({len(esc)})")
            lines += [f"- {e['id']} ({e['kind']}, {e['agent']}): {e['question'][:160]}" for e in esc]
        return "\n".join(lines)
