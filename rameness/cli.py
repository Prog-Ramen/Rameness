"""rameness command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import publish as pub
from .harness import Harness
from .tools import Approver


def _harness(a, need_llm=True) -> Harness:
    cfg = config_mod.load(overrides={"provider": getattr(a, "provider", None), "model": getattr(a, "model", None),
                                     "base_url": getattr(a, "base_url", None)})
    if getattr(a, "base_url", None) and not getattr(a, "provider", None):
        cfg.update(provider="openai", api_key_env=None, fast_model=cfg["model"] if getattr(a, "model", None) else None)
        if not getattr(a, "model", None):
            from .fleet.slots import Slot, probe_server
            s = probe_server(Slot("x", "openai", url=a.base_url))
            cfg["model"] = cfg["fast_model"] = s.model or "local"
    if getattr(a, "yes", False):
        cfg["permissions"]["mode"] = "auto"
    if getattr(a, "jev", None):
        cfg["jev"]["backend"] = a.jev
    if getattr(a, "no_learn", False):
        cfg["learning"]["enabled"] = False
    llm = None
    if not need_llm:
        from .llm import FakeProvider
        llm = FakeProvider()        # planning / SOP commands never call the model
        cfg["jev"]["backend"] = "lexical" if cfg["jev"]["backend"] in ("llm", "cascade") else cfg["jev"]["backend"]
    return Harness(cfg, llm=llm)


def _print_result(res, verbose: bool):
    print(res.text)
    m = res.metrics
    print(f"\n[{res.route}] llm_calls={m.get('llm_calls', 0)} tokens={m.get('input_tokens', 0)}in/"
          f"{m.get('output_tokens', 0)}out jev_calls={m.get('jev_calls', 0)} {m.get('seconds', 0)}s", file=sys.stderr)
    if verbose:
        print(res.plan.describe(), file=sys.stderr)


def _run_with_clarify(h: Harness, task: str, verbose: bool, interactive: bool = True):
    resolved: dict = {}
    for _ in range(4):
        res = h.run(task, resolved)
        if res.route != "clarify" or not interactive or not sys.stdin.isatty():
            return res
        for r in res.plan.questions:
            ans = input(f"? {r.question or f'Which {r.name}?'} ").strip()
            if ans:
                resolved[r.name] = ans
                task += f"\n({r.name}: {ans})"
    return res


def cmd_run(a):
    h = _harness(a)
    task = " ".join(a.task) if a.task else sys.stdin.read()
    _print_result(_run_with_clarify(h, task, a.verbose), a.verbose)


def cmd_chat(a):
    h = _harness(a)
    print(f"rameness {__version__} - {h.cfg['provider']}:{h.cfg['model']}  jev={h.jev.backend.name}  "
          f"sops={len(h.lib.sops)}  (ctrl-d to exit)", file=sys.stderr)
    while True:
        try:
            task = input("\n> ").strip()
        except EOFError:
            break
        if task:
            _print_result(_run_with_clarify(h, task, a.verbose), a.verbose)


def cmd_plan(a):
    h = _harness(a, need_llm=False)
    print(h.plan(" ".join(a.task)).describe())


def cmd_init(a):
    d = Path.cwd() / ".rameness"
    (d / "sops").mkdir(parents=True, exist_ok=True)
    if not (d / "config.json").exists():
        (d / "config.json").write_text(json.dumps({"provider": "anthropic", "permissions": {"mode": "ask"}}, indent=2) + "\n")
    if not (d / "org.json").exists():
        (d / "org.json").write_text(json.dumps({"name": "", "facts": [], "defaults": {}, "glossary": {},
                                                "private_terms": []}, indent=2) + "\n")
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("artifacts/\nruns/\ndecisions.jsonl\nsop_stats.json\n")
    print(f"initialised {d}")


def cmd_sop(a):
    h = _harness(a, need_llm=False)
    lib = h.lib
    if a.action == "tree":
        print(lib.tree())
    elif a.action == "list":
        for s in sorted(lib.sops.values(), key=lambda s: s.id):
            st = lib.stats.get(s.id, {})
            print(f"{s.id:32s} {s.kind:9s} {s.scope:7s} {s.status:9s} uses={st.get('uses', 0)} {s.description[:60]}")
    elif a.action == "show":
        s = lib.get(a.arg)
        print(json.dumps({**s.to_json(), "path": str(s.path), "scope": s.scope}, indent=2))
    elif a.action == "search":
        for s, p in lib.search(h.jev, a.arg):
            print(f"{p:.2f}  {s.interface()}")
    elif a.action == "run":
        print(json.dumps(h.executor.run(a.arg, json.loads(a.args or "{}")), indent=2))
    elif a.action == "test":
        ids = [a.arg] if a.arg else sorted(lib.sops)
        bad = 0
        for i in ids:
            if not lib.sops[i].tests:
                continue
            f = h.executor.test(i)
            bad += bool(f)
            print(f"{'FAIL' if f else 'ok  '} {i}" + "".join(f"\n     {x}" for x in f))
        sys.exit(1 if bad else 0)
    elif a.action == "promote":
        s = lib.get(a.arg)
        f = h.executor.test(a.arg) if s.tests else []
        if f and not a.force:
            sys.exit("tests fail:\n" + "\n".join(f))
        s.status = "validated"
        s.save()
        print(f"{s.id} -> validated")
    elif a.action == "remove":
        import shutil
        s = lib.get(a.arg)
        if s.scope != "private":
            sys.exit("only private SOPs can be removed; uninstall the package instead")
        shutil.rmtree(s.path)
        print(f"removed {s.id}")
    elif a.action == "publish":
        s = lib.get(a.arg)
        if not a.to:
            sys.exit("--to <package dir> is required")
        g = pub.generality(h.jev, s)
        findings = pub.scrub(s, h.org)
        print(f"generality (JEV, advisory): {g:.2f}")
        if findings:
            print("scrubber findings:\n  " + "\n  ".join(findings))
            if not a.force:
                sys.exit("refusing to publish; fix the SOP or pass --force after reviewing")
        print(f"files to publish: {[str(p.relative_to(s.path)) for p in s.path.rglob('*') if p.is_file()]}")
        if not a.yes and input("publish these files publicly? [y/N] ").strip().lower() != "y":
            sys.exit("aborted")
        print(f"published to {pub.publish(s, Path(a.to), h.org, force=a.force)}")
    elif a.action == "install":
        print(f"installed to {pub.install(a.arg, h.home, a.name)}")
    elif a.action == "index":
        print(f"wrote {pub.build_index(Path(a.arg))}")
    elif a.action == "remote":
        src = a.index or h.cfg["registry"]["remote_index"]
        if not src:
            sys.exit("--index <url or path> (or registry.remote_index in config) required")
        for e, p in pub.search_index(h.jev, src, a.arg):
            print(f"{p:.2f}  {e['id']:30s} {e['description'][:70]}")


def cmd_learn(a):
    h = _harness(a)
    from .learning import mine_repeats
    for c in mine_repeats(h.learner.runs.all(), h.cfg["learning"]["min_repeats"]):
        print(f"{c.count}x {'exact' if c.exact_repeat else 'shape'}  {c.name}")


# ---------------------------------------------------------------- fleet

def _fleet(probe=False):
    from .fleet.manager import Fleet
    return Fleet(Path.cwd(), probe=probe)


def _print_tree(node, prefix="", last=True, root=True):
    icon = {"running": "●", "starting": "◐", "blocked": "◆", "done": "✓", "failed": "✗", "queued": "○",
            "paused": "‖", "retired": "·"}.get(node["status"], "?")
    where = " @ ".join(x for x in (node.get("slot"), node.get("env")) if x)
    line = f"{icon} {node['id']}  [{node['role']}] {node['title'][:60]}" + (f"  ({where})" if where else "")
    print(line if root else prefix + ("└─ " if last else "├─ ") + line)
    kids = node["children"]
    for i, c in enumerate(kids):
        _print_tree(c, "" if root else prefix + ("   " if last else "│  "), i == len(kids) - 1, False)


def cmd_fleet(a):
    act = a.action
    if act == "up":
        from .fleet.server import serve
        f = _fleet(probe=True)
        if a.open:
            import webbrowser
            webbrowser.open(f"http://{a.host}:{a.port or f.cfg['port']}")
        serve(f, a.host, a.port or f.cfg["port"])
        return
    f = _fleet(probe=act in ("envs", "slots"))
    args = a.args
    if act == "ask":
        cycles = a.cycles if a.cycles == "godmode" else int(a.cycles or 0)
        r = f.ask(" ".join(args), cycles=cycles, categories=a.focus.split(",") if a.focus else None, on=a.on)
        print(json.dumps(r, indent=1, default=str))
        if r["route"] == "awaiting-director":
            print("JEV deferred this decision to you: see `rameness fleet needs`.", file=sys.stderr)
    elif act in ("tree", "ls"):
        _print_tree(f.tree())
    elif act == "show":
        ag = f._public(f.get(args[0]))
        print(json.dumps(ag, indent=1, default=str))
    elif act == "spawn":
        ag = f.spawn(" ".join(args), parent=a.parent or "manager", role=a.role or "associate",
                     kind=a.kind or "deliver", slot=a.slot, env=a.env, role_explicit=bool(a.role))
        print(f"queued {ag['id']} (starts when `rameness fleet up` is running)")
    elif act == "prompt":
        print(f.prompt(args[0], " ".join(args[1:])))
    elif act == "reassign":
        print(json.dumps(f._public(f.reassign(args[0], a.slot, a.env)), indent=1, default=str))
    elif act in ("pause", "resume"):
        getattr(f, act)(args[0])
        print(f"{act}d {args[0]}")
    elif act == "retire":
        f.retire(args[0], drop_branch=a.drop_branch)
        print(f"retired {args[0]}")
    elif act == "rm":
        f.retire(args[0], delete=True, drop_branch=a.drop_branch)
        print(f"deleted {args[0]}")
    elif act == "fork":
        print([x["id"] for x in f.fork(args[0], args[1:] or None, a.n)])
    elif act == "attach":
        h = f.get(args[0]).get("handle")
        if not h:
            sys.exit("no session for that agent")
        cmd = f.backend_for(h).attach_cmd(h)
        print(cmd, file=sys.stderr)
        os.execvp("bash", ["bash", "-c", cmd])
    elif act == "logs":
        h = f.get(args[0]).get("handle") or {}
        print(f.backend_for(h).read(h, a.lines) if h else "")
    elif act == "slots":
        for s in f.snapshot()["slots"]:
            print(f"{'●' if s['available'] else '○'} {s['id']:42s} {s['kind']:12s} {s['busy']}/{s['capacity']}  {s['model']}")
    elif act == "envs":
        for e in f.snapshot()["envs"]:
            i = e["info"]
            g = ", ".join(f"{x['name']} {x['mem_gb']}GB" for x in i.get("gpus", [])) or "no gpu"
            print(f"{e['id']:14s} {e['kind']:10s} {i.get('cpus', '?')} cpu {i.get('mem_gb', '?')}GB  {g}  "
                  f"{'' if i.get('reachable', True) else 'UNREACHABLE ' + i.get('error', '')}")
    elif act == "needs":
        for e in f.store.escalations():
            opts = [o for o in e["options"] if o != "__triaged"]
            rec = e["payload"].get("recommended")
            print(f"{e['id']}  ({e['kind']}, {e['agent']})\n  {e['question'][:600]}\n  options: {opts}"
                  + (f"  JEV recommends: {rec}" if rec else "") + "\n")
    elif act == "answer":
        f.answer(args[0], " ".join(args[1:]))
        print("answered")
    elif act == "shadow":
        import time as _t
        since = _t.time() - a.minutes * 60
        while True:
            for d in f.store.decisions_since(since):
                since = d["t"]
                top = sorted(d["probs"].items(), key=lambda kv: -kv[1])[:3]
                print(f"{_t.strftime('%H:%M:%S', _t.localtime(d['t']))} [{d['gate'] or 'jev':9s}] {d['agent'] or '-':10s} "
                      f"{d['question'][:60]} -> {d['chosen']}  " + " ".join(f"{k}={v:.2f}" for k, v in top), flush=True)
            if not a.follow:
                break
            _t.sleep(1)
    elif act == "mode":
        if args:
            print(f"autonomy: {f.set_autonomy(args[0])}")
        else:
            print(f"autonomy: {f.autonomy}  (restrictive | balanced | autopilot | godmode)")
    elif act == "programs":
        if args and args[0] == "stop":
            f.programs.stop(args[1])
            print(f"stopping {args[1]} after the current cycle")
            return
        for p in f.programs.all():
            total = p["total"] if p["total"] is not None else "∞"
            print(f"{p['id']}  {p['status']:8s} {p['done']}/{total}  phase={p['phase']:13s} {p['goal'][:60]}")
            for h in p["history"]:
                extra = f" findings={h['findings']} coverage={h.get('coverage')}" if h.get("mode") == "test" else ""
                print(f"    #{h['n']} {h['category']:22s} chosen={[o['title'][:30] for o in h['chosen']]}{extra}")
    elif act == "categories":
        from .fleet.cycles import TAXONOMY
        group = None
        for c in TAXONOMY:
            if c["group"] != group:
                group = c["group"]
                print(f"\n{group}  (use --focus {group.lower().replace('-', '_')} for the whole group)")
            print(f"  {c['id']:24s} {c['label']}")
    elif act == "standup":
        print(f.standup())
    elif act == "tick":
        f.tick()
        _print_tree(f.tree())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rameness", description="adaptive agent harness driven by a JEV decision model")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--provider", help="anthropic | openai | deepseek | ollama | llama-server")
    ap.add_argument("--model")
    ap.add_argument("--base-url", help="OpenAI-compatible endpoint, e.g. http://localhost:8080/v1 (llama-server)")
    ap.add_argument("--jev", help="lexical | llm | http | cascade | cascade-http")
    ap.add_argument("-y", "--yes", action="store_true", help="auto-approve tool actions")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run one task")
    p.add_argument("task", nargs="*")
    p.add_argument("--no-learn", action="store_true")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("chat", help="interactive session")
    p.add_argument("--no-learn", action="store_true")
    p.set_defaults(fn=cmd_chat)
    p = sub.add_parser("plan", help="show routing + SOP traversal without executing")
    p.add_argument("task", nargs="+")
    p.set_defaults(fn=cmd_plan)
    sub.add_parser("init", help="create ./.rameness").set_defaults(fn=cmd_init)
    sub.add_parser("learn", help="show recurring step patterns across runs").set_defaults(fn=cmd_learn)
    p = sub.add_parser("fleet", help="manager + team of agents (UI: `rameness fleet up`)")
    p.add_argument("action", choices=["up", "ask", "tree", "ls", "show", "spawn", "prompt", "reassign", "pause",
                                      "resume", "retire", "rm", "fork", "attach", "logs", "slots", "envs", "needs",
                                      "answer", "shadow", "standup", "tick", "mode", "programs", "categories"])
    p.add_argument("args", nargs="*")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int)
    p.add_argument("--open", action="store_true", help="open the UI in a browser")
    p.add_argument("--parent")
    p.add_argument("--role", choices=["associate", "lead"])
    p.add_argument("--kind", choices=["deliver", "research"])
    p.add_argument("--slot")
    p.add_argument("--env")
    p.add_argument("--n", type=int)
    p.add_argument("--lines", type=int, default=80)
    p.add_argument("--drop-branch", action="store_true")
    p.add_argument("-f", "--follow", action="store_true", help="shadow: keep following")
    p.add_argument("--minutes", type=float, default=60, help="shadow: history window")
    p.add_argument("--cycles", help="ask: number of refinement/testing cycles after the draft, or 'godmode'")
    p.add_argument("--focus", help="ask: comma-separated categories or groups (see `fleet categories`), e.g. testing,ui")
    p.add_argument("--on", help="ask: cycle on existing work instead of drafting: HEAD or an agent id")
    p.set_defaults(fn=cmd_fleet)
    p = sub.add_parser("up", help="shortcut for `fleet up --open`")
    p.set_defaults(fn=lambda a: cmd_fleet(argparse.Namespace(action="up", host="127.0.0.1", port=None, open=True)))
    p = sub.add_parser("sop", help="manage the SOP library")
    p.add_argument("action", choices=["tree", "list", "show", "search", "run", "test", "promote", "remove",
                                      "publish", "install", "index", "remote"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--args", help="JSON arguments for 'run'")
    p.add_argument("--to", help="package directory for 'publish'")
    p.add_argument("--name", help="package name for 'install'")
    p.add_argument("--index", help="registry index.json url/path for 'remote'")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_sop)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if a.verbose:
            raise
        sys.exit(f"rameness: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
