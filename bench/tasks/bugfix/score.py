"""Score the bugfix task: hidden tests (5 issues + 7 no-regression checks) on a copy of the agent's work,
plus the repo's own tests. Prints JSON: score (0-100), passed, total, checks {name: {pass, detail}}."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent

RUNNER = r'''
import json, sys, unittest
sys.path.insert(0, ".")
res = {}
class R(unittest.TextTestResult):
    def addSuccess(self, t): super().addSuccess(t); res[t.id().split(".")[-1]] = [True, ""]
    def addFailure(self, t, e): super().addFailure(t, e); res[t.id().split(".")[-1]] = [False, str(e[1])[:200]]
    def addError(self, t, e): super().addError(t, e); res[t.id().split(".")[-1]] = [False, repr(e[1])[:200]]
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])
unittest.TextTestRunner(resultclass=R, stream=open("/dev/null", "w")).run(suite)
print(json.dumps(res))
'''


def score(work: Path) -> dict:
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "w"
        shutil.copytree(work, d, ignore=shutil.ignore_patterns(".git", "__pycache__", ".rameness"))
        shutil.copy(HERE / "hidden" / "test_hidden.py", d / "test_hidden.py")
        (d / "_runner.py").write_text(RUNNER)
        checks = {}
        try:
            p = subprocess.run([sys.executable, "_runner.py", "test_hidden"], cwd=d, capture_output=True, text=True, timeout=120)
            for name, (ok, why) in json.loads(p.stdout.strip().splitlines()[-1]).items():
                checks[name] = {"pass": ok, "detail": why}
        except Exception as e:
            checks["hidden_tests_run"] = {"pass": False, "detail": repr(e)[:200]}
        own = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=d,
                             capture_output=True, text=True, timeout=120)
        checks["repo_tests_pass"] = {"pass": own.returncode == 0, "detail": own.stderr.strip().splitlines()[-1:]}
    passed = sum(c["pass"] for c in checks.values())
    issues = sum(c["pass"] for k, c in checks.items() if k.startswith("test_") and k[5].isdigit())
    return {"score": round(100 * passed / len(checks), 1), "passed": passed, "total": len(checks),
            "issues_fixed": issues, "checks": checks}


if __name__ == "__main__":
    print(json.dumps(score(Path(sys.argv[1])), indent=1))
