"""Learn SOPs from past runs: the model reviews them five at a time, Kev and the tests decide, as at the end of a run.

    python3 -m bench.mine_history --runs /home/bench/runs --home /root/bench/sop-mining \
        --base-url http://127.0.0.1:8000/v1 --model Qwen3.8-Flash-Next

Each run folder's ``events.jsonl`` (``tool_calls.jsonl`` for full inputs when present) becomes a history record:
the task, the steps, and what every model turn cost, so savings are measured. SOPs go to ``<home>/sops``, a staging
library next to the built-in SOPs: duplicates of a built-in are skipped, and a close built-in is extended as a
staged copy, never edited in place. Nothing is proposed; review the results before using them.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from rameness import config, jev as jev_mod
from rameness.learning import Learner, RunStore
from rameness.llm import OpenAICompatProvider, sampling_for
from rameness.sops import BUILTIN_ROOT, Executor, Library


def run_record(folder: Path) -> dict | None:
    """One past run as a history record (task, steps with their turn, per-turn cost)."""
    events = folder / "events.jsonl"
    if not events.exists():
        return None
    full = {}
    if (folder / "tool_calls.jsonl").exists():                  # untruncated inputs, keyed by (turn, n-th call)
        seen: dict[int, int] = {}
        for line in (folder / "tool_calls.jsonl").read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            n = seen.get(d.get("turn"), 0)
            seen[d.get("turn")] = n + 1
            full[(d.get("turn"), n)] = d.get("input")
    task, t0, steps, costs, per_turn = "", None, [], {}, {}
    for line in events.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t0 = t0 or e.get("t")
        kind = e.get("kind")
        if kind == "plan":
            task = e.get("task", "")
        elif kind == "llm":
            usage = e.get("usage") or {}
            costs[str(e.get("turn"))] = {"tokens": int(usage.get("output") or 0), "seconds": float(e.get("seconds") or 0)}
        elif kind == "tool":
            turn = e.get("turn")
            n = per_turn.get(turn, 0)
            per_turn[turn] = n + 1
            inp = full.get((turn, n))
            if not isinstance(inp, dict):
                try:
                    inp = json.loads(e.get("input") or "{}")
                except json.JSONDecodeError:
                    inp = {"command": str(e.get("input"))[:400]}   # cut off in the log: keep its start
            steps.append({"tool": e.get("tool"), "input": inp if isinstance(inp, dict) else {},
                          "ok": e.get("ok") in (True, "True"), "turn": turn})
    if not steps:
        return None
    return {"id": folder.name, "task": task, "steps": steps, "success": True, "project": str(folder / "work"),
            "turn_costs": costs, "t": t0 or 0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="/home/bench/runs")
    ap.add_argument("--home", default="/root/bench/sop-mining")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--model", default="Qwen3.8-Flash-Next")
    ap.add_argument("--window", type=int, default=5)
    a = ap.parse_args()
    home = Path(a.home)
    (home / "sops").mkdir(parents=True, exist_ok=True)
    records = sorted(filter(None, (run_record(f) for f in Path(a.runs).iterdir() if f.is_dir())), key=lambda r: r["t"])
    print(f"{len(records)} past runs, {sum(len(r['steps']) for r in records)} steps", flush=True)

    llm = OpenAICompatProvider(a.model, base_url=a.base_url)
    llm.sampling = sampling_for(a.model, "auto")
    cfg = config.load(home, {})
    cfg["jev"].update(backend="kev", kev_url="http://127.0.0.1:8008/v1/systemone")
    jev = jev_mod.build(cfg, None, home / "decisions.jsonl")
    lib = Library([(BUILTIN_ROOT, "public"), (home / "sops", "private")], home / "sop_stats.json")
    ex = Executor(lib, ["fs:read", "fs:write", "exec", "compute", "network"], cwd=home)
    learner = Learner(lib, ex, jev, RunStore(home / "runs"), llm)
    log = home / "mining.jsonl"
    done = set()
    if log.exists():                          # resume: windows already reviewed are skipped
        for line in log.read_text().splitlines():
            try:
                done.add(tuple(json.loads(line)["window"]))
            except (json.JSONDecodeError, KeyError):
                continue
    for i in range(0, len(records), a.window):
        window = records[i:i + a.window]
        if tuple(r["id"] for r in window) in done:
            continue
        t0 = time.time()
        cands = learner.review_runs(window)
        if cands:
            d = jev.activate("Which of these steps is a standard, reusable procedure likely to recur in future tasks?",
                             window[-1]["task"], [jev_mod.Option(str(k), c.text) for k, c in enumerate(cands)])
            for k, c in enumerate(cands):
                c.score = 1 - (1 - d.probs[str(k)]) * (1 - (1 - 2.718281828 ** -(c.count - 1)))
        results = learner._learn(window[-1]["task"], sorted(cands, key=lambda c: -c.score)) if cands else []
        row = {"window": [r["id"] for r in window], "seconds": round(time.time() - t0, 1),
               "candidates": [{"name": c.name, "count": c.count, "steps": len(c.steps), "score": round(c.score, 2),
                               "saves": c.saves} for c in cands], "results": results}
        with log.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        print(f"[{i // a.window + 1}/{-(-len(records) // a.window)}] {row['seconds']} s | "
              f"{len(cands)} candidates | " + "; ".join(
                  r.get("sop", "") and f"{'extended' if r.get('extended') else 'new'} {r['sop']} ({r['status']})"
                  or f"skipped {r.get('candidate')}: {r.get('skipped') or r.get('error')}" for r in results), flush=True)


if __name__ == "__main__":
    main()
