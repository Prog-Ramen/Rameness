"""Visual-quality judge: Claude grades each build's screenshot on a fixed rubric.

The black-box checks test behaviour (does it walk, break blocks, render something) but not how
it looks: a world full of missing faces still passes them. This adds a separate score.

    python -m bench.minecraft.judge /root/bench/results/minecraft/<batch> [--model sonnet]

Writes judge.json next to each run's result.json and pushes bench_run_judge_* to VictoriaMetrics.
"""

from __future__ import annotations

import shutil
import argparse
import json
import re
import subprocess
from pathlib import Path

from .. import vm

RUBRIC = """You are grading a screenshot of a browser game that was supposed to be a Minecraft clone.
Read the image file {path} and grade it. Score each 0-10:

- minecraft: how much it looks and feels like Minecraft (blocky voxel world, block textures, sky)
- rendering: correctness (0 = broken: missing or black faces, holes, z-fighting, garbled geometry;
  10 = clean)
- world: richness of the generated world (terrain shape, trees, water, varied blocks, distance)
- ui: game UI (crosshair, hotbar, HUD) present and tidy

Be strict and consistent. Reply with only a JSON object, no prose:
{{"minecraft": n, "rendering": n, "world": n, "ui": n, "notes": "one sentence"}}"""


CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")

def judge(shot: Path, model: str) -> dict:
    r = subprocess.run([CLAUDE, "-p", "--model", model, "--output-format", "json", "--allowedTools=Read",
                        RUBRIC.format(path=shot)], capture_output=True, text=True, timeout=300, cwd=shot.parent)
    out = json.loads(r.stdout)
    m = re.search(r"\{.*\}", out.get("result", ""), re.S)
    if not m:   # e.g. the claude CLI hit a usage limit and replied with an error message
        raise RuntimeError(f"judge reply has no JSON: {str(out.get('result', r.stdout))[:300]}")
    j = json.loads(m.group(0))
    j["overall"] = round(sum(j[k] for k in ("minecraft", "rendering", "world", "ui")) / 4, 2)
    j["judge_model"], j["judge_cost_usd"] = model, out.get("total_cost_usd")
    return j


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    for res in sorted(Path(a.batch).glob("*/result.json")):
        d = res.parent
        r = json.loads(res.read_text())
        out = d / "judge.json"
        if out.exists() and not a.force:
            j = json.loads(out.read_text())
        elif (d / "screenshot.png").exists():
            j = judge(d / "screenshot.png", a.model)
            out.write_text(json.dumps(j, indent=1))
        else:
            j = {k: 0 for k in ("minecraft", "rendering", "world", "ui", "overall")} | {"notes": "no build to render"}
            out.write_text(json.dumps(j, indent=1))
        labels = {"harness": r["harness"], "model": r["model"], "server": r["server"], "run": r["run"],
                  "batch": r["run"].rsplit(f"-{r['harness']}-", 1)[0]}
        vm.push([(f"bench_run_judge_{k}", j[k], labels) for k in ("minecraft", "rendering", "world", "ui", "overall")])
        print(f"{r['run']}: overall {j['overall']}  ({j.get('notes', '')})")


if __name__ == "__main__":
    main()
