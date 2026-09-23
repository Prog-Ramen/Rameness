"""Worker process for a rameness-runtime agent.

Runs inside the agent's session (tmux window, screen, herdr tab, or headless).
The model runs here; tools act on the agent's environment (local, ssh,
container...). Talks to the manager only through the fleet store:

* status / result / progress events
* ``ask_manager`` -> an escalation; the worker blocks until it is answered
* inbox messages from the director are injected between turns
* the transcript is saved so the agent can be resumed or forked
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .. import config as config_mod
from ..harness import Harness
from ..llm import ToolCall
from ..tools import Approver
from .manager import Fleet


def dump_messages(msgs: list[dict]) -> list[dict]:
    out = []
    for m in msgs:
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [{"id": c.id, "name": c.name, "input": c.input} for c in m["tool_calls"]]
        out.append(m)
    return out


def load_messages(data: list[dict], keep_raw: bool) -> list[dict]:
    out = []
    for m in data:
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [ToolCall(c["id"], c["name"], c["input"]) for c in m["tool_calls"]]
        if not keep_raw:
            m.pop("raw", None)
        out.append(m)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--id", required=True)
    a = ap.parse_args(argv)
    root = Path(a.root)
    fleet = Fleet(root, probe=False, planner=False)       # no planner needed inside a worker
    store = fleet.store
    agent = store.agent(a.id)
    aid = agent["id"]
    slot = fleet.slot(agent["slot"])
    env = fleet.envs.get(agent["env"] or "local", fleet.envs["local"])
    if env.kind != "local":
        env.probe()
    if slot is None:
        store.update_agent(aid, result=f"slot {agent['slot']} is not available")
        store.set_status(aid, "failed")
        return 1
    from .slots import provider_for
    llm = provider_for(slot)

    cfg = config_mod.load(root)
    cfg["permissions"]["mode"] = fleet.cfg["worker_permissions"]
    cfg["allow_direct"] = False
    workdir = agent["worktree"] or (env.workdir if env.kind != "local" else str(root))

    def ask_manager(args: dict):
        eid = store.escalate(aid, args["question"], args.get("options") or [], kind="agent-question")
        store.set_status(aid, "blocked", args["question"][:120])
        print(f"[{aid}] waiting on manager: {args['question']}", flush=True)
        deadline = time.time() + 3600
        while time.time() < deadline:
            e = store.escalation(eid)
            if e and e["status"] == "answered":
                store.set_status(aid, "running")
                return f"Answer: {e['answer']}", False
            time.sleep(2)
        store.set_status(aid, "running")
        return "No answer within an hour; proceed with your best judgement and note the assumption.", False

    extra = {"ask_manager": ({
        "name": "ask_manager",
        "description": "Ask your manager a question you cannot resolve yourself (requirements, approvals, "
                       "choices that are not yours). Blocks until answered. Do not use for things you can look up.",
        "input_schema": {"type": "object", "properties": {"question": {"type": "string"},
                                                          "options": {"type": "array", "items": {"type": "string"}}},
                         "required": ["question"]}}, ask_manager)}
    team = "\n".join(f"- {s['id']} ({s['status']}): {s['title']}" for s in store.children(agent["parent"] or "manager")
                     if s["id"] != aid)
    brief = (f"# Your role\nYou are {aid}, an associate on a rameness team. Your worktree/branch: "
             f"{agent['branch'] or '(shared checkout: do not modify files unless the task says so)'}.\n"
             f"Teammates:\n{team or '- none'}\n"
             "Work autonomously. Use ask_manager only for decisions that are not yours. When done, reply with a "
             "short report: what you changed, how you verified it, open issues.")
    h = Harness(cfg, llm=llm, approver=Approver(cfg["permissions"]["mode"]), env=env, workdir=workdir,
                extra_tools=extra, system_extra=brief, out=lambda s: print(s, flush=True))
    h.inbox = lambda: [f"[message from {m['sender']}] {m['text']}" for m in store.inbox(aid)]

    def on_turn(turn, r):
        store.event(aid, "turn", f"#{turn + 1} {r.text[:160] or ', '.join(c.name for c in r.tool_calls)}")
        store.update_agent(aid, meta={**store.agent(aid)["meta"], "turns": turn + 1,
                                      "tokens": llm.usage["input"] + llm.usage["output"]})
    h.on_turn = on_turn

    def on_decision(kind, d, detail):
        # loop-guard (and future in-agent) decisions appear in the manager's shadow view
        store.decision(d.id, aid, d.question, d.probs, detail.get("action", d.best), d.backend,
                       query="; ".join(detail.get("signals", [])), gate="jev", reason=f"{kind} guard")
        store.event(aid, "loop-guard", f"{detail.get('action')}: {'; '.join(detail.get('signals', []))[:300]}")
    h.on_decision = on_decision

    tdir = root / ".rameness" / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    tfile = tdir / f"{aid}.json"
    task = agent["task"]
    src = agent["meta"].get("transcript_from")
    if agent["meta"].get("resume") and tfile.exists():
        h.messages = load_messages(json.loads(tfile.read_text()), keep_raw=True)
        task = "Continue the task from where you left off." + (
            "" if not store.db.execute("SELECT 1 FROM messages WHERE agent=? AND delivered=0", (aid,)).fetchone()
            else " New instructions follow.")
    elif src and (tdir / f"{src}.json").exists():
        h.messages = load_messages(json.loads((tdir / f"{src}.json").read_text()), keep_raw=False)
        task = f"You are now a parallel attempt forked from that work. {agent['meta'].get('variant', '')}\n\n{task}"

    print(f"[{aid}] {slot.id} @ {env.id}:{workdir}", flush=True)
    store.set_status(aid, "running")
    try:
        res = h.run(task, allow_direct=False)
        ok = res.metrics.get("success", False)
        store.update_agent(aid, result=res.text, meta={**store.agent(aid)["meta"], "resume": False,
                                                       "metrics": res.metrics, "learned": res.learned})
        store.set_status(aid, "done" if ok else "failed", "" if ok else "did not finish cleanly")
    except Exception as e:
        store.update_agent(aid, result=f"{type(e).__name__}: {e}")
        store.set_status(aid, "failed", f"{type(e).__name__}: {e}"[:200])
        raise
    finally:
        tfile.write_text(json.dumps(dump_messages(h.messages), default=str))
    print(f"[{aid}] finished: {store.agent(aid)['status']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
