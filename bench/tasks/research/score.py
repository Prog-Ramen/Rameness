"""Score the research task: deterministic checks on report.md against the sources' facts, plus a separate
0-10 quality grade from a Claude judge (reported, not part of the score). Prints JSON."""
import shutil
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SOURCES = {p.name for p in (HERE / "seed" / "sources").glob("*.md")}


CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")

def money(n: int) -> str:
    k = n // 1000
    return rf"\$?\s?({n:,}|{n}|{k}\s?[kK]\b|{k} ?000|{k}\.0\s?[kK])"


def near(text: str, a: str, b: str, span: int = 160) -> bool:
    """a and b within `span` characters of each other, in either order."""
    return bool(re.search(rf"{a}.{{0,{span}}}?{b}|{b}.{{0,{span}}}?{a}", text, re.I | re.S))


VENDORS = ("helix", "kestrel", "otter")


def vendor_has(text: str, vendor: str, num: str) -> bool:
    """`num` (as a count, not hours or a percentage) stated for `vendor`: within 80 characters after a mention, or 40
    before it, with no other vendor named in between."""
    others = [v for v in VENDORS if v != vendor]
    n = rf"(?<![\d.,$])\b{num}\b(?![.,]\d)(?!\s*(hours|hour|h\b|%|°|picks))"
    for m in re.finditer(vendor, text, re.I):
        after = text[m.end():m.end() + 80]
        cut = min([i for i in (after.lower().find(o) for o in others) if i >= 0] or [len(after)])
        if re.search(n, after[:cut], re.I):
            return True
        before = text[max(0, m.start() - 40):m.start()]
        cut = max([i + len(o) for o in others if (i := before.lower().rfind(o)) >= 0] or [0])
        if re.search(n, before[cut:], re.I):
            return True
    return False


def checks_for(r: str) -> dict:
    c = {}
    c["q1_kestrel_measured_vs_datasheet"] = bool(re.search("kestrel", r, re.I)) and "455" in r and "510" in r
    c["q2_helix_units_9"] = vendor_has(r, "helix", "9")
    c["q2_kestrel_units_8"] = vendor_has(r, "kestrel", "8")
    c["q2_otter_units_10"] = vendor_has(r, "otter", "10")
    c["q3_fleet_costs"] = all(re.search(money(n), r) for n in (432000, 488000, 395000))
    c["q3_maintenance"] = all(re.search(money(n), r) for n in (36000, 52000, 30000))
    c["q4_otter_not_cold"] = near(r, "otter", r"(not|cannot|isn.t|no)\b.{0,60}?(cold|certif)", 200)
    c["q5_forum_distrusted"] = near(r, "forum", r"(unverified|anonymous|unreliable|not trust|distrust|disregard|discount|outlier|anecdot)", 250)
    c["q6_recommendation"] = bool(re.search(r"recommend", r, re.I))
    cited = set(re.findall(r"[A-Za-z0-9_\-]+\.md", r)) - {"report.md"}
    c["cites_5_real_sources"] = len(cited & SOURCES) >= 5
    c["no_invented_sources"] = not (cited - SOURCES)
    return c


def judge(report: Path) -> dict:
    prompt = (f"Read the files in {HERE / 'seed' / 'sources'} and then the report {report}, written to answer the "
              f"brief in {HERE / 'task.md'}. Grade the report 0-10 for: accuracy (every claim matches the sources), "
              "reasoning (sound handling of conflicts and trade-offs), and usefulness (clear, direct, well cited). "
              'Reply with only JSON: {"accuracy": n, "reasoning": n, "usefulness": n, "notes": "one sentence"}')
    try:
        p = subprocess.run([CLAUDE, "-p", "--model", "sonnet", "--output-format", "json", "--allowedTools=Read",
                            prompt], capture_output=True, text=True, timeout=300)
        j = json.loads(re.search(r"\{.*\}", json.loads(p.stdout)["result"], re.S).group(0))
        j["overall"] = round((j["accuracy"] + j["reasoning"] + j["usefulness"]) / 3, 2)
        return j
    except Exception as e:
        return {"overall": None, "notes": f"judge failed: {e!r}"[:200]}


def score(work: Path, use_judge: bool = True) -> dict:
    rep = work / "report.md"
    checks = {"report_exists": {"pass": rep.exists(), "detail": ""}}
    text = rep.read_text(errors="replace") if rep.exists() else ""
    for k, v in checks_for(text).items():
        checks[k] = {"pass": bool(v), "detail": ""}
    passed = sum(c["pass"] for c in checks.values())
    out = {"score": round(100 * passed / len(checks), 1), "passed": passed, "total": len(checks), "checks": checks,
           "words": len(text.split())}
    if use_judge and rep.exists():
        out["judge"] = judge(rep)
    return out


if __name__ == "__main__":
    print(json.dumps(score(Path(sys.argv[1]), use_judge="--no-judge" not in sys.argv), indent=1))
