"""Recompute derived per-run fields for finished runs and push them to VictoriaMetrics.

    python -m bench.backfill /root/bench/results/minecraft/<batch> [...]

Adds compactions (from the recorded requests), harness-logged compactions, the model's context
window and the run's context utilisation to runs recorded before those fields existed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import vm
from .llmproxy import mark_compactions
from .minecraft.run import MODELS, context_window, logged_compactions


def backfill(batch: Path) -> None:
    by_id = {m["id"]: m for m in MODELS.values()}
    for res in sorted(batch.glob("*/result.json")):
        r = json.loads(res.read_text())
        recs = [json.loads(ln) for ln in (res.parent / "llm.jsonl").read_text().splitlines()] \
            if (res.parent / "llm.jsonl").exists() else []
        m = by_id.get(r["model"], {})
        window, slots = context_window(m) if m else (None, None)
        run_dir = (Path("/home/bench/runs") if not m.get("hosted") else batch) / r["run"]
        r.update(compactions=mark_compactions(recs), context_window=window, slots=slots,
                 context_utilization=round(r["context_tokens_max"] / window, 3) if window and r.get("context_tokens_max") else None,
                 compactions_logged=logged_compactions(r["harness"], run_dir, res.parent))
        res.write_text(json.dumps(r, indent=1))
        labels = {"harness": r["harness"], "model": r["model"], "server": r["server"], "run": r["run"],
                  "batch": r["run"].rsplit(f"-{r['harness']}-", 1)[0]}
        vm.push([("bench_run_compactions", r["compactions"], labels),
                 ("bench_run_compactions_logged", r["compactions_logged"], labels),
                 ("bench_run_context_window", window, labels),
                 ("bench_run_context_utilization", r["context_utilization"], labels),
                 ("bench_model_context_window", window, {"model": r["model"], "server": r["server"]}),
                 ("bench_model_slots", slots, {"model": r["model"], "server": r["server"]})])
        print(f"{r['run']}: window {window}, max context {r.get('context_tokens_max')} "
              f"({r['context_utilization']}), compactions {r['compactions']} / logged {r['compactions_logged']}")


if __name__ == "__main__":
    for b in sys.argv[1:]:
        backfill(Path(b))
