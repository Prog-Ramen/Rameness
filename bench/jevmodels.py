"""Benchmark JEV's decision models (Laya, Kev, Open-Jev) and push everything to VictoriaMetrics.

Models run one at a time (each server is started, measured, and stopped) because together they
don't fit in RAM on CPU. Per model it records:

* every decision: latency, correct or not, probability on the right option, confidence
* accuracy and mean P(correct) per decision set, on standard and paraphrased wording
* latency p50 / p95 / max, and batched Noul latency for 1 / 4 / 8 / 16 questions per request
  (how per-option ``activate`` decisions scale)
* server load time and resident memory

    python -m bench.jevmodels --models laya,kev,openjev --device cpu
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
import urllib.request
from pathlib import Path

from rameness import jevbench
from rameness.jev import Jev, Option, SystemOneJev, laya_running

from . import vm

HOME = Path.home()
MODELS = {
    "laya": {
        "url": "http://127.0.0.1:8000/v1/systemone", "model": "typed-decisions", "params": "0.42B",
        "cmd": [str(HOME / ".rameness/env/bin/laya-serve")],
        "env": {"LAYA_HOST": "127.0.0.1", "LAYA_PORT": "8000", "LAYA_MODELS": "typed-decisions", "LAYA_PRELOAD": "1"},
        "cpu_env": {"LAYA_DEVICE": "cpu"},
    },
    "kev": {
        "url": "http://127.0.0.1:8008/v1/systemone", "model": "kev", "params": "4B",
        "cmd": [str(HOME / ".rameness/jev/kev/src/.venv/bin/python"), "-m", "kev.serve",
                "--run", "jaredpalmer/kev-4b", "--port", "8008"],
        "cwd": str(HOME / ".rameness/jev/kev/src"), "env": {}, "cpu_env": {"CUDA_VISIBLE_DEVICES": ""},
    },
    "openjev": {
        "url": "http://127.0.0.1:8791/v1/systemone", "model": None, "params": "2B",   # serves one model; names are rejected
        "cmd": ["/root/bench/openjev/.venv/bin/python", "-m", "jev.server",
                "--checkpoint", "/root/bench/openjev-2b/package/checkpoint", "--max-length", "1024",
                "--batch-size", "8", "--port", "8791"],
        "cwd": "/root/bench/openjev", "env": {}, "cpu_env": {"CUDA_VISIBLE_DEVICES": ""}, "device_flag": True,
    },
}


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


def bench(name: str, device: str, run: str, out: Path) -> dict:
    m = MODELS[name]
    base = {"jev_model": name, "checkpoint": m["model"] or "open-jev-2b", "params": m["params"], "device": device, "run": run}
    log = out / f"{name}-server.log"
    print(f"== {name}: starting on {device}", flush=True)
    p, load_s = start(name, device, log)
    rss0 = rss_bytes(p.pid)
    print(f"   answering after {load_s:.0f}s, RSS {rss0 / 2**30:.1f} GiB", flush=True)
    vm.push([("bench_jev_load_seconds", load_s, base), ("bench_jev_rss_bytes", rss0, base)])
    backend = SystemOneJev("laya", url=m["url"], model=m["model"], timeout=300)
    backend.name, backend.model = name, m["model"]       # None: send no model name (Open-Jev rejects unknown names)
    jev = Jev(backend)
    rows, lat = [], []
    for phrasing, cases in (("standard", jevbench.CASES), ("paraphrased", jevbench.PARAPHRASED)):
        for set_name, state, gold in cases:
            question, opts = jevbench.SETS[set_name]
            f0 = backend.failures
            t0 = time.time()
            d = jev.choose(question, state, opts)
            dt = time.time() - t0
            failed = backend.failures > f0
            ok = (d.best == gold) and not failed
            lab = {**base, "set": set_name, "phrasing": phrasing}
            if not failed:
                lat.append(dt)
            rows.append({"set": set_name, "phrasing": phrasing, "state": state, "gold": gold, "got": d.best,
                         "ok": ok, "p_gold": d.probs[gold], "conf": d.top(1)[0][1], "seconds": dt, "failed": failed,
                         "error": backend.last_error if failed else None})
            vm.push([("bench_jev_decision_seconds", dt, lab), ("bench_jev_decision_correct", int(ok), lab),
                     ("bench_jev_decision_p_gold", d.probs[gold], lab),
                     ("bench_jev_decision_confidence", d.top(1)[0][1], lab),
                     ("bench_jev_decision_failed", int(failed), lab)])
    # batched Noul questions: the cost of one per-option `activate`
    batch = {}
    for k in (1, 4, 8, 16):
        opts = [Option(f"o{i}", f"capability {i}: files git http csv json data test deploy") for i in range(k)]
        f0 = backend.failures
        t0 = time.time()
        backend.activate("Which of these capabilities will this task need?", "summarize sales.csv and commit it", opts)
        batch[k] = None if backend.failures > f0 else time.time() - t0
        vm.push([("bench_jev_batch_seconds", batch[k], {**base, "questions": k})])
    rss1 = rss_bytes(p.pid)
    stop(p)
    summary = {"model": name, "device": device, "load_seconds": round(load_s, 1), "rss_gib": round(max(rss0, rss1) / 2**30, 2),
               "failed": sum(r["failed"] for r in rows), "batch_seconds": batch}
    agg = []
    for phrasing in ("standard", "paraphrased"):
        rs = [r for r in rows if r["phrasing"] == phrasing]
        acc = sum(r["ok"] for r in rs) / len(rs)
        pg = sum(r["p_gold"] for r in rs) / len(rs)
        summary[phrasing] = {"accuracy": round(acc, 3), "p_gold": round(pg, 3)}
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
    vm.push(agg)
    (out / f"{name}-decisions.json").write_text(json.dumps(rows, indent=1))
    print("   " + json.dumps(summary), flush=True)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="laya,kev,openjev")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--out", default="/root/bench/results/jev")
    a = ap.parse_args()
    run = time.strftime("jev-%Y%m%d-%H%M%S")
    out = Path(a.out) / run
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for name in a.models.split(","):
        try:
            results.append(bench(name, a.device, run, out))
        except Exception as e:
            print(f"   {name} failed: {e}", flush=True)
            results.append({"model": name, "error": str(e)})
    (out / "summary.json").write_text(json.dumps(results, indent=1))
    print(f"results: {out}")


if __name__ == "__main__":
    sys.exit(main())
