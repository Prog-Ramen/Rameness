"""Where the benchmarks keep their files. Override with environment variables:

  RAMENESS_BENCH          the bench folder (results, A/B summaries, helper environments); default ~/bench
  RAMENESS_BENCH_RUNS     where runs execute (each run's working folder); default <bench>/runs
  RAMENESS_BENCH_PUBLISH  optional folder results are copied to after a batch (e.g. a shared results server)
  RAMENESS_BENCH_PYTHON   the Python that runs the Minecraft scorer (needs Playwright); default this Python
"""
import os
import shutil
import sys
from pathlib import Path

BENCH = Path(os.environ.get("RAMENESS_BENCH", "~/bench")).expanduser()
RUNS = Path(os.environ.get("RAMENESS_BENCH_RUNS", str(BENCH / "runs"))).expanduser()
RESULTS = BENCH / "results"
PUBLISH = os.environ.get("RAMENESS_BENCH_PUBLISH") or None
PYTHON = os.environ.get("RAMENESS_BENCH_PYTHON") or sys.executable
CLAUDE = shutil.which("claude") or "claude"
