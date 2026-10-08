"""Generate the projreport tasks: projreport1..8, each a folder of six small Python projects to report on.

    python3 -m bench.tasks.projreport.make

The same procedure (count files and lines, collect TODOs, run the tests) repeats for every project in a task
and again in every task, so it measures whether Rameness turns it into an SOP and saves time and tokens:
within a run (mid-run learning) and across runs (a shared Rameness home).
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE.parent
NAMES = ["inventory", "billing", "routing", "metrics", "scheduler", "parser", "mailer", "cache", "auth", "search",
         "ledger", "queue", "geo", "media", "notify", "reports", "sessions", "uploads", "weather", "chat"]
TODOS = ["handle empty input", "add retries", "log the failure", "validate the range", "support unicode",
         "cache this lookup", "remove the hard-coded limit", "check permissions", "close the file", "add a timeout"]

TASK_MD = """The folder `projects/` holds several small Python projects. Write `report.json` in the current directory
with one entry per project folder:

```json
{"<project>": {"py_files": 0, "lines": 0, "todos": ["path/in/project.py:LINE: text"], "tests_pass": true}}
```

- `py_files`: the number of `.py` files in the project (including its tests), at any depth.
- `lines`: the total number of lines in those `.py` files.
- `todos`: every comment containing `TODO`, as `relative/path.py:LINE: text after "TODO:"`, sorted.
- `tests_pass`: whether `python3 -m unittest discover -s tests -q`, run inside the project folder, exits with 0.
"""

SCORE_PY = '''"""Score a projreport task: report.json against the hidden truth, one check per project and field."""
import json
import sys
from pathlib import Path

TRUTH = json.loads((Path(__file__).parent / "hidden" / "report.json").read_text())


def score(work: Path) -> dict:
    checks = {}
    try:
        got = json.loads((work / "report.json").read_text())
        checks["report_json_valid"] = {"pass": isinstance(got, dict), "detail": ""}
    except Exception as e:
        got, checks["report_json_valid"] = {}, {"pass": False, "detail": repr(e)[:200]}
    for project, want in TRUTH.items():
        have = got.get(project) if isinstance(got, dict) else None
        have = have if isinstance(have, dict) else {}
        for field, value in want.items():
            h = have.get(field)
            ok = sorted(h) == value if field == "todos" and isinstance(h, list) else h == value
            checks[f"{project}.{field}"] = {"pass": bool(ok), "detail": f"expected {value}, got {h}"}
    passed = sum(c["pass"] for c in checks.values())
    return {"score": round(100 * passed / len(checks), 1), "passed": passed, "total": len(checks), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(score(Path(sys.argv[1])), indent=2))
'''


def project(root: Path, name: str, rnd: random.Random) -> None:
    pkg = root / name
    (pkg / "tests").mkdir(parents=True)
    funcs = []
    for m in range(rnd.randint(2, 4)):
        body = [f'"""{name} module {m}."""', ""]
        for f in range(rnd.randint(2, 5)):
            fn = f"{name}_{m}_{f}"
            funcs.append((f"mod{m}", fn))
            body += [f"def {fn}(x):", f'    """Return x scaled by {f + 1}."""']
            if rnd.random() < 0.35:
                body.append(f"    # TODO: {rnd.choice(TODOS)}")
            body += [f"    return x * {f + 1}", ""]
        (pkg / f"mod{m}.py").write_text("\n".join(body))
    broken = rnd.random() < 0.4
    tests = ["import os", "import sys", "import unittest", "",
             "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))", ""]
    for mod, fn in funcs[:3]:
        tests.append(f"from {mod} import {fn}")
    tests += ["", "", "class Basics(unittest.TestCase):"]
    for i, (_, fn) in enumerate(funcs[:3]):
        want = 3 if (broken and i == 0) else 2 * int(fn.rsplit("_", 1)[1]) + 2
        tests += [f"    def test_{i}(self):", f"        self.assertEqual({fn}(2), {want})", ""]
    if rnd.random() < 0.3:
        tests.insert(0, "# TODO: cover the error cases")
    (pkg / "tests" / "test_basics.py").write_text("\n".join(tests))


def truth(root: Path) -> dict:
    out = {}
    for pkg in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(pkg.rglob("*.py"))
        todos = []
        for f in files:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if "TODO" in line and "#" in line:
                    todos.append(f"{f.relative_to(pkg)}:{i}: {line.split('TODO:', 1)[-1].strip()}")
        ok = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=pkg,
                            capture_output=True).returncode == 0
        out[pkg.name] = {"py_files": len(files), "lines": sum(len(f.read_text().splitlines()) for f in files),
                         "todos": sorted(todos), "tests_pass": ok}
    return out


def main() -> None:
    for k in range(1, 9):
        rnd = random.Random(1000 + k)
        task = TASKS / f"projreport{k}"
        shutil.rmtree(task, ignore_errors=True)
        root = task / "seed" / "projects"
        root.mkdir(parents=True)
        for name in rnd.sample(NAMES, 6):
            project(root, name, rnd)
        (task / "hidden").mkdir()
        (task / "hidden" / "report.json").write_text(json.dumps(truth(root), indent=2) + "\n")
        (task / "task.md").write_text(TASK_MD)
        (task / "score.py").write_text(SCORE_PY)
        print(task.name, {n: (v["py_files"], v["lines"], len(v["todos"]), v["tests_pass"])
                          for n, v in json.loads((task / "hidden" / "report.json").read_text()).items()})


if __name__ == "__main__":
    main()
