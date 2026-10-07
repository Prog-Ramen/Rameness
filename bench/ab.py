"""A/B a Rameness config change on the short tasks: control (defaults) vs treatment (a config overlay),
interleaved, same code, same model server, one run at a time.

    python3 -m bench.ab --name edit-fuzzy --config '{"edit_fuzzy": true}' [--tasks bugfix,research,data] [--n 2]

Writes /root/bench/ab/<name>.json and <name>.md with per-task means, spread and wall time.
Keep rule: treatment mean score >= control overall, no task worse than the control's own spread,
no more than 20% slower, and no loss of research quality. Missing scores, times or research grades
make the evidence incomplete. An eligible assessment still needs review before changing defaults.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import subprocess
import sys
from pathlib import Path

OUT = Path("/root/bench/ab")
RESULTS = Path("/root/bench/results")


def assess(rows: list[dict], tasks: list[str], n: int) -> dict:
    """Apply the keep rule only to a complete, finite set of paired results.

    Research also needs its separate quality grade: keyword scores alone cannot establish quality.
    A pass makes a change eligible for review; it does not change harness defaults.
    """
    failures, missing = [], []
    expected = {(task, arm, i) for task in tasks for arm in ("control", "treatment") for i in range(1, n + 1)}
    seen = {}

    def finite(value, lo, hi=math.inf):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and lo <= value <= hi

    for row in rows:
        key = (row.get("task"), row.get("arm"), row.get("i"))
        if key not in expected or key in seen:
            missing.append(f"Unexpected or duplicate run: {key}")
        seen[key] = row
    for key in sorted(expected):
        row = seen.get(key, {})
        if not finite(row.get("score"), 0, 100) or not finite(row.get("wall_seconds"), 0) or row.get("wall_seconds") == 0:
            missing.append(f"Missing or invalid score/time: {key}")
        if key[0] == "research" and not finite(row.get("judge"), 0, 10):
            missing.append(f"Missing or invalid research quality grade: {key}")

    def compare(field, label):
        groups = {arm: [seen.get((task, arm, i), {}).get(field)
                        for task in tasks for i in range(1, n + 1)] for arm in ("control", "treatment")}
        if not all(finite(v, 0) for values in groups.values() for v in values):
            return
        control, treatment = (st.mean(groups[a]) for a in ("control", "treatment"))
        if (field == "wall_seconds" and treatment > control * 1.2) or (field != "wall_seconds" and treatment < control):
            failures.append(f"{label}: control {control:.2f}, treatment {treatment:.2f}")

    if expected:
        compare("score", "Overall mean score regressed")
        compare("wall_seconds", "Mean wall time increased by more than 20%")
    else:
        missing.append("No runs requested")
    for task in tasks:
        for field in (["score", "judge"] if task == "research" else ["score"]):
            groups = {arm: [seen.get((task, arm, i), {}).get(field) for i in range(1, n + 1)]
                      for arm in ("control", "treatment")}
            if not n or not all(finite(v, 0) for values in groups.values() for v in values):
                continue
            control, treatment = groups["control"], groups["treatment"]
            if st.mean(treatment) < st.mean(control) - (max(control) - min(control)):
                failures.append(f"{task} {field} regressed beyond the control spread")
            if field == "judge" and st.mean(treatment) < st.mean(control):
                failures.append("Research mean quality grade regressed")
    return {"status": "reject" if failures else "incomplete" if missing else "eligible",
            "failures": failures, "missing": missing}


def run(task: str, batch: str, model: str, config: str, timeout: int) -> dict:
    cmd = [sys.executable, "-m", "bench.minecraft.run", "--task", task, "--runs", f"rameness:{model}",
           "--timeout", str(timeout), "--batch", batch]
    if config:
        cmd += ["--rameness-config", config]
    subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], check=False)
    f = RESULTS / task / batch / f"{batch}-rameness-{model}" / "result.json"
    return json.loads(f.read_text()) if f.exists() else {"score": None, "wall_seconds": None}


def summarise(name: str, config: str, rows: list[dict], tasks: list[str] | None = None, n: int | None = None) -> str:
    out = [f"# A/B: {name}", "", f"Treatment config: `{config}`", "",
           "| task | control scores | treatment scores | control mean | treatment mean | control min | treatment min |",
           "|---|---|---|---|---|---|---|"]
    tot = {"control": [], "treatment": []}
    wall = {"control": [], "treatment": []}
    for task in sorted({r["task"] for r in rows}):
        s = {arm: [r["score"] for r in rows if r["task"] == task and r["arm"] == arm and r["score"] is not None]
             for arm in ("control", "treatment")}
        for arm in s:
            tot[arm] += s[arm]
            wall[arm] += [r["wall_seconds"] / 60 for r in rows if r["task"] == task and r["arm"] == arm and r["wall_seconds"]]
        m = {a: (st.mean(v) if v else float("nan")) for a, v in s.items()}
        out.append(f"| {task} | {s['control']} | {s['treatment']} | {m['control']:.1f} | {m['treatment']:.1f} | "
                   f"{min(s['control'] or [0]):.1f} | {min(s['treatment'] or [0]):.1f} |")
    mc, mt = (st.mean(tot[a]) if tot[a] else float("nan") for a in ("control", "treatment"))
    wc, wt = (st.mean(wall[a]) if wall[a] else float("nan") for a in ("control", "treatment"))
    out += ["", f"Overall mean score: control {mc:.1f}, treatment {mt:.1f} (delta {mt - mc:+.1f})",
            f"Mean wall time: control {wc:.1f} min, treatment {wt:.1f} min ({(wt / wc - 1) * 100 if wc else 0:+.0f}%)"]
    assessment = assess(rows, tasks if tasks is not None else sorted({r["task"] for r in rows}),
                        n if n is not None else max((r["i"] for r in rows), default=0))
    out += ["", f"Assessment: **{assessment['status']}** (eligible means ready for review, not automatically adopted)."]
    out += [f"- {reason}" for reason in assessment["failures"] + assessment["missing"]]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--config", required=True, help="JSON overlay for the treatment arm")
    ap.add_argument("--control-config", default="", help="JSON overlay for the control arm (default: none)")
    ap.add_argument("--tasks", default="bugfix,research,data")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--model", default="qwen")
    ap.add_argument("--timeout", type=int, default=2700)
    a = ap.parse_args()
    tasks = [t.strip() for t in a.tasks.split(",")]
    if a.n < 1 or not all(tasks) or len(set(tasks)) != len(tasks):
        ap.error("--n must be positive and --tasks must contain unique, nonempty task names")
    json.loads(a.config)
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(1, a.n + 1):
        for task in tasks:
            for arm, cfg in (("control", a.control_config), ("treatment", a.config)):
                batch = f"ab-{a.name}-{arm}-{i}"
                r = run(task, batch, a.model, cfg, a.timeout)
                rows.append({"task": task, "arm": arm, "i": i, "score": r.get("score"), "wall_seconds": r.get("wall_seconds"),
                             "judge": r.get("judge")})
                (OUT / f"{a.name}.json").write_text(json.dumps(rows, indent=1))
                (OUT / f"{a.name}.md").write_text(summarise(a.name, a.config, rows, tasks, a.n))
    print((OUT / f"{a.name}.md").read_text())


if __name__ == "__main__":
    main()
