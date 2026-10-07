"""Copy the Minecraft bench's run records into the repo, small enough to keep in git.

    python3 -m bench.minecraft.archive [--results /root/bench/results/minecraft] [--bench /root/bench]

Writes ``bench/minecraft/runs/<batch>/<run>/`` and ``bench/minecraft/launchers/``. Per run it keeps the scores
(``result.json``), the judge's verdict (``judge.json``), the event log with long fields cut
(``events.jsonl.gz``), the per-request model log (``llm.jsonl.gz``) and the generated game's ``index.html``. It
leaves out what is large and either reproducible or recorded elsewhere: the rest of the generated game
(``build/``, including any ``node_modules``), screenshots, raw console streams and full tool inputs. LAN addresses and the host name are replaced, so the copy can be published.

Re-running is safe: every file is rewritten from the source.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import socket
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIELD_CHARS = 300          # a string field longer than this keeps its start and says how much was cut
# Private-network addresses and this machine's name (read at run time, so the name is not in the source).
SCRUB = [(re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "<lan-host>"),
         (re.compile(re.escape(socket.gethostname())), "<host>")]


def scrub(text: str) -> str:
    for rx, repl in SCRUB:
        text = rx.sub(repl, text)
    return text


def cut(value):
    if isinstance(value, str):
        return value if len(value) <= FIELD_CHARS else f"{value[:FIELD_CHARS]}... [{len(value)} chars, cut]"
    if isinstance(value, list):
        return [cut(v) for v in value]
    if isinstance(value, dict):
        return {k: (v if k == "task" else cut(v)) for k, v in value.items()}
    return value


def write_jsonl_gz(src: Path, dst: Path, trim: bool) -> None:
    with open(src, errors="replace") as f, gzip.open(dst, "wt", compresslevel=9) as out:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.write(scrub(json.dumps(cut(row) if trim else row, ensure_ascii=False)) + "\n")


def archive_run(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("result.json", "judge.json"):
        if (src / name).exists():
            (dst / name).write_text(scrub((src / name).read_text(errors="replace")))
    if (src / "events.jsonl").exists():
        write_jsonl_gz(src / "events.jsonl", dst / "events.jsonl.gz", trim=True)
    if (src / "llm.jsonl").exists():
        write_jsonl_gz(src / "llm.jsonl", dst / "llm.jsonl.gz", trim=False)
    if (src / "build" / "index.html").exists():
        # the game's entry page only; its other files (and any node_modules) stay on the bench machine
        (dst / "index.html").write_text(scrub((src / "build" / "index.html").read_text(errors="replace")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="/root/bench/results/minecraft")
    ap.add_argument("--bench", default="/root/bench", help="where the launcher scripts and their logs live")
    a = ap.parse_args()
    results, bench = Path(a.results), Path(a.bench)

    runs = HERE / "runs"
    for batch in sorted(p for p in results.iterdir() if p.is_dir()):
        for run in sorted(p for p in batch.iterdir() if p.is_dir()):
            if (run / "result.json").exists() or (run / "events.jsonl").exists():
                archive_run(run, runs / batch.name / run.name)
        if (batch / "results.json").exists():
            (runs / batch.name).mkdir(parents=True, exist_ok=True)
            (runs / batch.name / "results.json").write_text(scrub((batch / "results.json").read_text()))

    # The scripts that queued the runs, and their logs (only those that ran this benchmark).
    launchers = HERE / "launchers"
    launchers.mkdir(exist_ok=True)
    candidates = list(bench.glob("chain-*.sh")) + list(bench.glob("chain-*.log")) + \
        list((bench / "results").glob("mc-*.log")) + list((bench / "compare").glob("*.sh")) + \
        list((bench / "compare").glob("*.py"))
    for f in sorted(candidates):
        text = f.read_text(errors="replace")
        if not re.search(r"bench\.minecraft|bench/minecraft|\bmc-", text):
            continue
        sub = "compare" if f.parent.name == "compare" else ("batch-logs" if f.parent.name == "results" else "")
        (launchers / sub).mkdir(exist_ok=True)
        (launchers / sub / f.name).write_text(scrub(text))
    for report in (bench / "overnight-report.md", bench / "compare" / "speed-summary.md"):
        if report.exists():
            (HERE / "reports").mkdir(exist_ok=True)
            (HERE / "reports" / report.name).write_text(scrub(report.read_text()))


if __name__ == "__main__":
    main()
