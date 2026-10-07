"""Summarise benchmark results as Markdown tables.

    python -m bench.report --minecraft /root/bench/results/minecraft/<batch> [--jev /root/bench/results/jev]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def fmt_s(s):
    if s is None:
        return "-"
    return f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s"


def fmt_k(n):
    return "-" if n is None else (f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n))


def minecraft(batch: Path) -> str:
    rows = []
    for f in sorted(batch.glob("*/result.json")):
        r = json.loads(f.read_text())
        j = f.parent / "judge.json"
        r["visual"] = json.loads(j.read_text()).get("overall") if j.exists() else None
        rows.append(r)
    rows.sort(key=lambda r: (-(r.get("score") or 0), r.get("wall_seconds") or 0))
    out = ["| Harness | Model | Think budget | Score | Visual /10 | Checks | Time | Requests | Tool calls | Max context | Prompt sent (new) | Output tok | Gen tok/s | FPS | Lines | Cost |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append("| {harness} | {model_label} | {rb} | {score:.0f}% | {visual} | {passed}/14 | {time}{to} | {req} | {tools} | {ctx} | {pt} | {ct} | {tps} | {fps} | {loc} | {cost} |".format(
            harness=r["harness"], model_label=r.get("model_label", r["model"]),
            rb=fmt_k(r.get("reasoning_budget")) if r.get("reasoning_budget") else "-", score=r.get("score") or 0,
            visual="-" if r.get("visual") is None else f"{r['visual']:.1f}",
            passed=r.get("passed") or 0, time=fmt_s(r.get("wall_seconds")), to=" (limit)" if r.get("timed_out") else "",
            req=r.get("llm_requests", "-"), tools=r.get("tool_calls_total", "-"), ctx=fmt_k(r.get("context_tokens_max")),
            pt=f'{fmt_k(r.get("prompt_tokens_total"))} ({fmt_k((r.get("prompt_tokens_total") or 0) - (r.get("cached_tokens_total") or 0))})',
            ct=fmt_k(r.get("completion_tokens_total")),
            tps=f"{r['gen_tps_mean']:.0f}" if r.get("gen_tps_mean") else "-", fps=r.get("fps") or "-",
            loc=r.get("loc") or 0, cost=f"${r['cost_usd']:.2f}" if r.get("cost_usd") else "-"))
    checks = sorted({c for r in rows for c in r.get("checks", {})})
    if checks:
        out += ["", "| Run | " + " | ".join(c.replace("_", " ") for c in checks) + " |",
                "|---|" + "---|" * len(checks)]
        for r in rows:
            out.append(f"| {r['harness']} / {r.get('model_label', r['model'])} | " +
                       " | ".join("✓" if r.get("checks", {}).get(c) else "·" for c in checks) + " |")
    return "\n".join(out)


def jev(root: Path) -> str:
    best: dict[str, dict] = {}
    for f in sorted(root.glob("*/summary.json")):
        for r in json.loads(f.read_text()):
            if "error" not in r and not r.get("failed"):
                best[r["model"]] = r                      # latest successful run per model
    out = ["| Decision model | Standard | Paraphrased | P(correct) | p50 | p95 | 16-question batch | Memory | Load |",
           "|---|---|---|---|---|---|---|---|---|"]
    for m, r in sorted(best.items(), key=lambda kv: -kv[1]["standard"]["accuracy"]):
        lat = r.get("latency", {})
        out.append(f"| {m} | {r['standard']['accuracy']:.2f} | {r['paraphrased']['accuracy']:.2f} | "
                   f"{(r['standard']['p_gold'] + r['paraphrased']['p_gold']) / 2:.2f} | {lat.get('p50', 0):.2f} s | "
                   f"{lat.get('p95', 0):.2f} s | {r['batch_seconds'].get('16') or 0:.2f} s | {r['rss_gib']:.1f} GiB | "
                   f"{r['load_seconds']:.0f} s |")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minecraft")
    ap.add_argument("--jev")
    a = ap.parse_args()
    if a.jev:
        print(jev(Path(a.jev)) + "\n")
    if a.minecraft:
        print(minecraft(Path(a.minecraft)))
