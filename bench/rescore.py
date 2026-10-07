"""Re-grade finished runs with the current scorer, from each run's saved build.

    python -m bench.rescore /root/bench/results/minecraft/<batch> [...]

Updates result.json (score, checks, fps, ...), the screenshot and the bench_run_* metrics, and
drops judge.json when the score or screenshot changed so `bench.minecraft.judge` grades it again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import vm
from .minecraft.run import find_game, score_build


def rescore(batch: Path) -> None:
    for res in sorted(batch.glob("*/result.json")):
        d = res.parent
        r = json.loads(res.read_text())
        game = find_game(d / "build") if (d / "build").is_dir() else None
        if not game:
            print(f"{r['run']}: no build, kept {r.get('score')}")
            continue
        s = score_build(game, d / "screenshot.png")
        if "error" in s:
            print(f"{r['run']}: scorer failed: {s['error'][-300:]}")
            continue
        old = r.get("score")
        r.update(score=s.get("score"), passed=s.get("passed"), fps=s.get("fps"), loc=s.get("loc"),
                 files=s.get("files"), page_errors=len(s.get("page_errors") or []),
                 checks={k: v["pass"] for k, v in (s.get("checks") or {}).items()}, score_detail=s)
        res.write_text(json.dumps(r, indent=1))
        (d / "judge.json").unlink(missing_ok=True)       # the screenshot was retaken
        labels = {"harness": r["harness"], "model": r["model"], "server": r["server"], "run": r["run"],
                  "batch": r["run"].rsplit(f"-{r['harness']}-", 1)[0]}
        vm.push([(f"bench_run_{k}", r[k], labels) for k in ("score", "passed", "fps", "loc", "files", "page_errors")]
                + [("bench_run_check_pass", int(v), {**labels, "check": k}) for k, v in r["checks"].items()])
        print(f"{r['run']}: {old} -> {r['score']} (runs {s['score_runs']})" + (f" (pressed '{s['start_button']}')" if s.get("start_button") else ""))


if __name__ == "__main__":
    for b in sys.argv[1:]:
        rescore(Path(b))
