"""Review a pull request to a public SOP registry (the RamenSOPs CI gate).

The registry can't trust that ``rameness sop propose`` ran on the contributor's machine, so CI
re-checks every changed SOP, and on top of that asks whether it is safe for *other people* to run:

* scope: only ``sops/**`` may change, and generated indexes are rebuilt on merge, not committed
* structure: ``sop.json`` parses, its id matches its path, kind and permissions are known
* hard scrubber findings (secrets, gitleaks/trufflehog hits) fail; soft ones go to a human
* declared permissions cover what the code visibly does (network, exec, fs:write)
* the SOP has tests and they pass (run this inside a network-less sandbox: it is untrusted code)

A PR that passes is **auto-merge eligible** only if every changed SOP is new, is a script, and
needs no permission beyond ``low_risk`` - by default the same set rameness runs without asking.
Anything else passes to a human (``needs-human``): changes to existing SOPs, skills (instructions
injected into an agent's prompt), composites, deletions, and SOPs with real power.

``static=True`` skips the tests so the trusted merge job can recheck eligibility without
executing PR code.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .jev import Jev, LexicalJev
from .publish import hard_findings, private_info, scrub_tree
from .sops import SOP, Executor, Library

KINDS = ("script", "composite", "skill")
PERMISSIONS = ("fs:read", "fs:write", "network", "exec", "side-effect", "compute")
LOW_RISK = ("fs:read", "compute")
GENERATED = re.compile(r"(^|/)(_index|index)\.json$")

# what the code visibly does -> the permission it must declare
PY_USES = {
    "network": re.compile(r"^\s*(import|from)\s+(requests|httpx|socket|urllib3|aiohttp|ftplib|smtplib)\b"
                          r"|urllib\.request|http\.client|urlopen\(", re.M),
    "exec": re.compile(r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|pty\.spawn)\b|(?<![\w.])(eval|exec)\("),
    "fs:write": re.compile(r"open\([^)]*['\"][wax]\+?b?['\"]|\.write_(text|bytes)\(|\bshutil\.(rmtree|move|copy\w*)\("
                           r"|\bos\.(remove|unlink|rename|replace|makedirs|mkdir|rmdir)\("),
}
SH_USES = {
    "network": re.compile(r"\b(curl|wget|nc|ssh|scp|rsync)\b|/dev/tcp/"),
    "exec": re.compile(r"."),                                          # a shell script executes commands by definition
    "fs:write": re.compile(r"(^|[^\d&<>-])>>?\s*(?!&|/dev/null)\S|\b(tee|rm|mv|cp|mkdir|touch|dd)\b"),
}

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) for every file the PR changes relative to its merge base with ``base``."""
    out = git(repo, "diff", "--name-status", "--no-renames", f"{base}...HEAD")
    return [(ln.split("\t", 1)[0], ln.split("\t", 1)[1]) for ln in out.splitlines() if ln.strip()]


def owning_sop(repo: Path, path: str) -> Path | None:
    """The SOP directory a changed file belongs to (nearest ancestor with a sop.json)."""
    p = (repo / path).parent
    while p != repo and repo in p.parents:
        if (p / "sop.json").exists():
            return p
        p = p.parent
    return None


def used_permissions(sop_dir: Path) -> set[str]:
    used = set()
    for f in sorted(sop_dir.rglob("*")):
        if f.is_file() and f.suffix in (".py", ".sh"):
            code = "\n".join(ln for ln in f.read_text(errors="replace").splitlines() if not ln.lstrip().startswith("#"))
            used |= {perm for perm, rx in (PY_USES if f.suffix == ".py" else SH_USES).items() if rx.search(code)}
    return used

def review(repo: Path, base: str = "origin/main", static: bool = False, low_risk=LOW_RISK,
           timeout: int = 60) -> dict:
    repo = repo.resolve()
    fail, human, sops = [], [], []
    changes = changed_files(repo, base)
    if not changes:
        fail.append("the PR changes nothing")
    dirs: dict[Path, bool] = {}                                     # sop dir -> existed at base
    for status, path in changes:
        if not path.startswith("sops/"):
            fail.append(f"{path}: outside sops/ - SOP PRs may only change SOPs")
        elif GENERATED.search(path):
            fail.append(f"{path}: generated index - don't commit it, it is rebuilt on merge")
        elif status.startswith("D"):
            human.append(f"{path}: deleted")
        elif path.endswith("/_node.json"):
            continue                                                # category metadata: checked by structure below
        else:
            d = owning_sop(repo, path)
            if d is None:
                fail.append(f"{path}: not inside an SOP directory (no sop.json)")
            elif d not in dirs:
                existed = subprocess.run(["git", "cat-file", "-e", f"{base}:{(d / 'sop.json').relative_to(repo)}"],
                                         cwd=repo, capture_output=True).returncode == 0
                dirs[d] = existed

    root = repo / "sops"
    lib = Library([(root, "public")]) if root.exists() else None
    jev = Jev(LexicalJev())
    for d, existed in sorted(dirs.items()):
        sid = ".".join(d.relative_to(root).parts)
        rec = {"id": sid, "new": not existed, "permissions": [], "kind": None, "tests": None}
        sops.append(rec)
        try:
            sop = SOP.load(d, sid, "public")
        except (json.JSONDecodeError, TypeError) as e:
            fail.append(f"{sid}: sop.json does not load ({e})")
            continue
        rec.update(kind=sop.kind, permissions=list(sop.permissions))
        if sop.id != sid:
            fail.append(f"{sid}: sop.json id is {sop.id!r}, expected {sid!r} from its path")
        if not sop.description.strip():
            fail.append(f"{sid}: no description")
        if sop.kind not in KINDS:
            fail.append(f"{sid}: unknown kind {sop.kind!r}")
        unknown = [p for p in sop.permissions if p not in PERMISSIONS]
        if unknown:
            fail.append(f"{sid}: unknown permissions {unknown}")
        if sop.kind == "script" and not (d / sop.entry).exists():
            fail.append(f"{sid}: entry {sop.entry} is missing")

        findings = scrub_tree(d)
        hard = hard_findings(findings)
        fail += [f"{sid}: {f}" for f in hard]
        leak = private_info(jev, sop, findings) if not hard else {"leak": False, "findings": []}
        human += [f"{sid}: scrubber: {f}" + (" (JEV: looks private)" if leak["leak"] else "")
                  for f in leak["findings"]]

        undeclared = used_permissions(d) - set(sop.permissions)
        if undeclared:
            fail.append(f"{sid}: code uses {sorted(undeclared)} but does not declare it")

        if sop.kind == "script" and not sop.tests:
            fail.append(f"{sid}: no tests")
        elif not static and sop.kind == "script" and lib and sid in lib.sops:
            errs = Executor(lib, allow=list(PERMISSIONS), cwd=repo, timeout=timeout).test(sid)
            rec["tests"] = "fail" if errs else "pass"
            fail += [f"{sid}: {e}" for e in errs]

        if existed:
            human.append(f"{sid}: changes an existing SOP")
        if sop.kind != "script":
            human.append(f"{sid}: {sop.kind} SOP" + (" (instructions go into an agent's prompt)" if sop.kind == "skill" else ""))
        risky = [p for p in sop.permissions if p not in low_risk]
        if risky:
            human.append(f"{sid}: needs {risky}")

    ok = not fail
    return {"ok": ok, "auto_merge": ok and not human and bool(sops), "fail": fail, "human": human, "sops": sops,
            "static": static}


def markdown(r: dict) -> str:
    verdict = ("❌ **Checks failed**" if not r["ok"] else
               "✅ **Checks passed - eligible for auto-merge**" if r["auto_merge"] else
               "👀 **Checks passed - needs a maintainer's review**")
    out = [verdict, ""]
    if r["sops"]:
        out += ["| SOP | new | kind | permissions | tests |", "|---|---|---|---|---|"]
        out += [f"| `{s['id']}` | {'yes' if s['new'] else 'no'} | {s['kind'] or '?'} | "
                f"{', '.join(s['permissions']) or '-'} | {s['tests'] or ('skipped' if r['static'] else '-')} |"
                for s in r["sops"]]
        out.append("")
    if r["fail"]:
        out += ["**Must fix:**"] + [f"- {x}" for x in r["fail"]] + [""]
    if r["human"]:
        out += ["**Why a human has to look:**"] + [f"- {x}" for x in r["human"]] + [""]
    return "\n".join(out)
