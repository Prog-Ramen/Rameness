"""Benchmark JEV's decision models (Clef, Kev, Laya, Open-Jev, and the lexical baseline) and push
everything to VictoriaMetrics.

Models run one at a time (each server is started, measured, and stopped) because together they
don't fit in RAM on CPU. Clef is GPU-only and is measured on the server already running
(`rameness jev up clef`), which is never stopped. Each model is asked with the options written as
keywords, as sentences, or both (`--styles`); by default each gets the style Rameness uses for it.
Per model and style it records:

* every decision: latency, correct or not, probability on the right option, confidence
* accuracy and mean P(correct) per decision set, on standard and paraphrased wording
* latency p50 / p95 / max, and batched Noul latency for 1 / 4 / 8 / 16 questions per request
  (how per-option ``activate`` decisions scale)
* server load time and resident memory

    python -m bench.jevmodels --models clef,kev,laya,openjev,lexical --device cpu --styles both

The results (per-decision JSON, summary.json and a Markdown table) go to bench/results/jev/<run>/.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from rameness import jevbench
from rameness.jev import Jev, LexicalJev, Option, SystemOneJev, laya_running

from . import vm
from .paths import BENCH, RESULTS

HOME = Path.home()
# style: how Rameness writes the options for this model (jev.build); attach: measure the running server, never
# start or stop it; preset: SystemOneJev's preset (keeps its state budget)
MODELS = {
    "clef": {
        "url": "http://127.0.0.1:8010/v1/systemone", "model": "clef-flash", "params": "9B (8-bit)",
        "style": "sentences", "attach": True, "preset": "clef", "device": "cuda",
    },
    "lexical": {"model": None, "params": "none", "style": "keywords", "in_process": True, "device": "cpu"},
    "laya": {
        "url": "http://127.0.0.1:8000/v1/systemone", "model": "typed-decisions", "params": "0.42B",
        "cmd": [str(HOME / ".rameness/env/bin/laya-serve")],
        "env": {"LAYA_HOST": "127.0.0.1", "LAYA_PORT": "8000", "LAYA_MODELS": "typed-decisions", "LAYA_PRELOAD": "1"},
        "cpu_env": {"LAYA_DEVICE": "cpu"}, "style": "keywords", "preset": "laya",
    },
    "kev": {
        "url": "http://127.0.0.1:8008/v1/systemone", "model": "kev", "params": "4B",
        "cmd": [str(HOME / ".rameness/jev/kev/src/.venv/bin/python"), "-m", "kev.serve",
                "--run", "jaredpalmer/kev-4b", "--port", "8008"],
        "cwd": str(HOME / ".rameness/jev/kev/src"), "env": {}, "cpu_env": {"CUDA_VISIBLE_DEVICES": ""},
        "style": "keywords", "preset": "kev",
    },
    "openjev": {
        "url": "http://127.0.0.1:8791/v1/systemone", "model": None, "params": "2B",   # serves one model; names are rejected
        "cmd": [str(BENCH / "openjev/.venv/bin/python"), "-m", "jev.server",
                "--checkpoint", str(BENCH / "openjev-2b/package/checkpoint"), "--max-length", "1024",
                "--batch-size", "8", "--port", "8791"],
        "cwd": str(BENCH / "openjev"), "env": {}, "cpu_env": {"CUDA_VISIBLE_DEVICES": ""}, "device_flag": True,
        "style": "keywords", "preset": "laya",
    },
}


def listener_pid(url: str) -> int | None:
    """The process listening on the URL's port (for an attached server's memory)."""
    port = url.split(":")[2].split("/")[0]
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True).stdout
    for part in out.split("pid=")[1:]:
        return int(part.split(",")[0])
    return None


def gpu_mib(pid: int) -> int:
    """GPU memory held by a process (0 without a GPU or nvidia-smi)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(int(m) for p_, m in (ln.split(", ") for ln in out.splitlines() if ", " in ln) if int(p_) == pid)


def rss_bytes(pid: int) -> int:
    """Resident memory of a process and its children."""
    total = 0
    for p in [pid] + [int(x) for x in subprocess.run(["pgrep", "-P", str(pid)], capture_output=True,
                                                   text=True).stdout.split()]:
        try:
            for ln in Path(f"/proc/{p}/status").read_text().splitlines():
                if ln.startswith("VmRSS:"):
                    total += int(ln.split()[1]) * 1024
        except OSError:
            pass
    return total


def start(name: str, device: str, log: Path) -> tuple[subprocess.Popen, float]:
    m = MODELS[name]
    env = {**os.environ, **m["env"], **(m["cpu_env"] if device == "cpu" else {})}
    cmd = list(m["cmd"]) + (["--device", "cpu" if device == "cpu" else "cuda:0"] if m.get("device_flag") else [])
    t0 = time.time()
    p = subprocess.Popen(cmd, cwd=m.get("cwd"), env=env, stdout=log.open("ab"), stderr=subprocess.STDOUT,
                         start_new_session=True)
    while time.time() - t0 < 1800:
        if p.poll() is not None:
            raise RuntimeError(f"{name} exited ({p.returncode}); see {log}")
        if laya_running(m["url"], timeout=30):
            return p, time.time() - t0
        time.sleep(3)
    raise RuntimeError(f"{name} did not answer within 30 min")


def stop(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=60)
    except Exception:
        os.killpg(p.pid, signal.SIGKILL)


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def bench(name: str, device: str, run: str, out: Path, styles: list[str]) -> list[dict]:
    """Start the model (or attach to it), measure it in each option style, stop it."""
    m = MODELS[name]
    device = m.get("device") if (m.get("attach") or m.get("in_process")) else device
    p, pid, load_s = None, None, None
    if m.get("in_process"):
        print(f"== {name}: in-process", flush=True)
    elif m.get("attach"):
        if not laya_running(m["url"], timeout=30, model=m["model"]):
            raise RuntimeError(f"{name} is not running at {m['url']} (start it with `rameness jev up {name}`)")
        pid = listener_pid(m["url"])
        print(f"== {name}: measuring the running server (pid {pid})", flush=True)
    else:
        print(f"== {name}: starting on {device}", flush=True)
        p, load_s = start(name, device, out / f"{name}-server.log")
        pid = p.pid
        print(f"   answering after {load_s:.0f}s, RSS {rss_bytes(pid) / 2**30:.1f} GiB", flush=True)
    try:
        if m.get("in_process"):          # the lexical scorer reads keywords whatever the style
            styles = []
        return [measure(name, m, device, run, out, style, pid, load_s)
                for style in (styles or [m["style"]])]
    finally:
        if p:
            stop(p)


def measure(name: str, m: dict, device: str, run: str, out: Path, style: str, pid: int | None,
            load_s: float | None) -> dict:
    base = {"jev_model": name, "checkpoint": m["model"] or ("open-jev-2b" if name == "openjev" else name),
            "params": m["params"], "device": device, "run": run, "style": style}
    rss0 = rss_bytes(pid) if pid else 0
    if load_s is not None:
        vm.push([("bench_jev_load_seconds", load_s, base)])
    if m.get("in_process"):
        backend = LexicalJev()
    else:
        backend = SystemOneJev(m["preset"], url=m["url"], model=m["model"], timeout=300, option_text=style)
        backend.name, backend.model = name, m["model"]   # None: send no model name (Open-Jev rejects unknown names)
    jev = Jev(backend)
    failures = lambda: getattr(backend, "failures", 0)   # noqa: E731
    rows, lat = [], []
    for phrasing, cases in (("standard", jevbench.CASES), ("paraphrased", jevbench.PARAPHRASED)):
        for set_name, state, gold in cases:
            question, opts = jevbench.SETS[set_name]
            f0 = failures()
            t0 = time.time()
            d = jev.choose(question, state, opts)
            dt = time.time() - t0
            failed = failures() > f0
            ok = (d.best == gold) and not failed
            lab = {**base, "set": set_name, "phrasing": phrasing}
            if not failed:
                lat.append(dt)
            rows.append({"set": set_name, "phrasing": phrasing, "state": state, "gold": gold, "got": d.best,
                         "ok": ok, "p_gold": d.probs[gold], "conf": d.top(1)[0][1], "seconds": dt, "failed": failed,
                         "error": getattr(backend, "last_error", None) if failed else None})
            vm.push([("bench_jev_decision_seconds", dt, lab), ("bench_jev_decision_correct", int(ok), lab),
                     ("bench_jev_decision_p_gold", d.probs[gold], lab),
                     ("bench_jev_decision_confidence", d.top(1)[0][1], lab),
                     ("bench_jev_decision_failed", int(failed), lab)])
    # batched Noul questions: the cost of one per-option `activate`
    batch = {}
    for k in (1, 4, 8, 16):
        opts = [Option(f"o{i}", f"capability {i}: files git http csv json data test deploy") for i in range(k)]
        f0 = failures()
        t0 = time.time()
        backend.activate("Which of these capabilities will this task need?", "summarize sales.csv and commit it", opts)
        batch[k] = None if failures() > f0 else round(time.time() - t0, 4)
        vm.push([("bench_jev_batch_seconds", batch[k], {**base, "questions": k})])
    rss1 = rss_bytes(pid) if pid else 0
    summary = {"model": name, "style": style, "device": device, "params": m["params"],
               "load_seconds": round(load_s, 1) if load_s is not None else None,
               "rss_gib": round(max(rss0, rss1) / 2**30, 2), "gpu_gib": round(gpu_mib(pid) / 1024, 2) if pid else 0,
               "failed": sum(r["failed"] for r in rows),
               "batch_seconds": batch}
    agg = []
    for phrasing in ("standard", "paraphrased"):
        rs = [r for r in rows if r["phrasing"] == phrasing]
        acc = sum(r["ok"] for r in rs) / len(rs)
        pg = sum(r["p_gold"] for r in rs) / len(rs)
        summary[phrasing] = {"n": len(rs), "accuracy": round(acc, 3), "p_gold": round(pg, 3)}
        agg += [("bench_jev_accuracy", acc, {**base, "phrasing": phrasing, "set": "all"}),
                ("bench_jev_p_gold", pg, {**base, "phrasing": phrasing, "set": "all"})]
        for s in sorted({r["set"] for r in rs}):
            ss = [r for r in rs if r["set"] == s]
            agg += [("bench_jev_accuracy", sum(r["ok"] for r in ss) / len(ss), {**base, "phrasing": phrasing, "set": s}),
                    ("bench_jev_p_gold", sum(r["p_gold"] for r in ss) / len(ss), {**base, "phrasing": phrasing, "set": s})]
    if lat:
        summary["latency"] = {"p50": round(statistics.median(lat), 3), "p95": round(pct(lat, 0.95), 3),
                              "max": round(max(lat), 3), "mean": round(statistics.mean(lat), 3)}
        agg += [("bench_jev_latency_seconds", summary["latency"][q], {**base, "quantile": q}) for q in ("p50", "p95", "max", "mean")]
        agg.append(("bench_jev_throughput_dps", len(lat) / sum(lat), base))
    agg += [("bench_jev_rss_bytes", max(rss0, rss1), base), ("bench_jev_failed_decisions", summary["failed"], base)]
    summary["sets"] = {s: {"n": len(ss), "accuracy": round(sum(r["ok"] for r in ss) / len(ss), 3),
                           "p_gold": round(sum(r["p_gold"] for r in ss) / len(ss), 3)}
                       for s in sorted({r["set"] for r in rows}) for ss in [[r for r in rows if r["set"] == s]]}
    summary["misses"] = [{k: r[k] for k in ("set", "phrasing", "state", "gold", "got")} for r in rows if not r["ok"]]
    vm.push(agg)
    (out / f"{name}-{style}-decisions.json").write_text(json.dumps(rows, indent=1))
    print("   " + json.dumps(summary), flush=True)
    return summary


def table(results: list[dict]) -> str:
    """The comparison as Markdown: overall, then accuracy per decision kind."""
    ok = [r for r in results if "error" not in r]
    lines = ["| Model | Options as | Device | Accuracy | Paraphrased | P(correct) | Per decision (p50 / p95) | RAM / GPU |",
             "|---|---|---|---|---|---|---|---|"]
    for r in ok:
        lat = r.get("latency") or {}
        lines.append(f"| {r['model']} ({r['params']}) | {r['style']} | {r['device']} | {r['standard']['accuracy']:.2f} | "
                     f"{r['paraphrased']['accuracy']:.2f} | {r['standard']['p_gold']:.2f} | "
                     f"{lat.get('p50', 0):.3f} / {lat.get('p95', 0):.3f} s | {r['rss_gib'] or '-'} / {r.get('gpu_gib') or '-'} GiB |")
    sets = sorted({s for r in ok for s in r["sets"]})
    lines += ["", "| Decision kind | n | " + " | ".join(f"{r['model']} ({r['style']})" for r in ok) + " |",
              "|---|---|" + "---|" * len(ok)]
    for s in sets:
        n = next(r["sets"][s]["n"] for r in ok if s in r["sets"])
        lines.append(f"| {s} | {n} | " + " | ".join(
            f"{r['sets'][s]['accuracy']:.2f} / {r['sets'][s]['p_gold']:.2f}" if s in r["sets"] else "-" for r in ok) + " |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="clef,kev,laya,openjev,lexical")
    ap.add_argument("--device", default="cpu", help="for the models this script starts (Clef is measured where it runs)")
    ap.add_argument("--styles", default="default",
                    help="default (the style Rameness uses for each model), keywords, sentences, or both")
    ap.add_argument("--out", default=str(RESULTS / "jev"))
    a = ap.parse_args()
    styles = {"default": [], "both": ["keywords", "sentences"]}.get(a.styles, [a.styles])
    run = time.strftime("jev-%Y%m%d-%H%M%S")
    out = Path(a.out) / run
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for name in a.models.split(","):
        try:
            results += bench(name, a.device, run, out, styles)
        except Exception as e:
            print(f"   {name} failed: {e}", flush=True)
            results.append({"model": name, "error": str(e)})
    (out / "summary.json").write_text(json.dumps(results, indent=1))
    (out / "summary.md").write_text(table(results))
    print(table(results))
    print(f"results: {out}")


if __name__ == "__main__":
    sys.exit(main())
