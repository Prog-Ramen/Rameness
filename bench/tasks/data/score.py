"""Score the data task: answers.json against the hidden truth (exact for counts and names, +-0.01 for money)."""
import json
import sys
from pathlib import Path

TRUTH = json.loads((Path(__file__).parent / "hidden" / "answers.json").read_text())


def score(work: Path) -> dict:
    checks = {}
    try:
        got = json.loads((work / "answers.json").read_text())
        checks["answers_json_valid"] = {"pass": isinstance(got, dict), "detail": ""}
    except Exception as e:
        got, checks["answers_json_valid"] = {}, {"pass": False, "detail": repr(e)[:200]}
    for k, want in TRUTH.items():
        have = got.get(k)
        if isinstance(want, float):
            try:
                ok = abs(float(have) - want) <= 0.011
            except (TypeError, ValueError):
                ok = False
        else:
            ok = str(have).strip().lower() == str(want).lower()
        checks[k] = {"pass": ok, "detail": f"expected {want}, got {have}"}
    passed = sum(c["pass"] for c in checks.values())
    return {"score": round(100 * passed / len(checks), 1), "passed": passed, "total": len(checks), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(score(Path(sys.argv[1])), indent=1))
