"""Score a projreport task: report.json against the hidden truth, one check per project and field."""
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
