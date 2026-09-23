"""Public SOP ecosystem: scrub, publish, index, search, install.

Private SOPs never leave the machine on their own. ``publish`` is always an
explicit, human-confirmed action and refuses anything the scrubber flags:
secrets, emails, private hosts/IPs, home paths, and the organization's own
``private_terms``. The JEV's "is this generic?" score is advisory only.

A registry is just a directory (or git repo) of packages plus ``index.json``,
a metadata-only index so remote discovery never downloads code.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .jev import Jev, Option
from .org import Org
from .sops import SOP, Library

PATTERNS = {
    "secret": re.compile(r"(?i)(api[_-]?key|secret|token|passw(or)?d|bearer|client[_-]?secret|auth)\s*[:=]\s*"
                         r"['\"]?[A-Za-z0-9_\-./+=]{8,}"),
    "key-like": re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}|"
                           r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|"
                           r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20})"),
    "private-key": re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "url-credentials": re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "private-ip": re.compile(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b"),
    "private-host": re.compile(r"\b[\w-]+(\.[\w-]+)*\.(internal|local|corp|lan|intranet)\b"),
    "home-path": re.compile(r"(/home/[\w.-]+|/Users/[\w.-]+|C:\\\\Users\\\\)"),
}
# categories that can never be overridden with --force
SECRET_KINDS = ("secret", "key-like", "private-key", "jwt", "url-credentials")
TEXT_SUFFIXES = (".py", ".sh", ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env", ".js", ".ts", "")


def scrub_text(text: str, terms: list[str], label: str) -> list[str]:
    out = []
    for kind, rx in PATTERNS.items():
        for m in rx.finditer(text):
            out.append(f"{label}: {kind}: {m.group(0)[:60]}")
    low = text.lower()
    out += [f"{label}: private term: {t}" for t in terms if t and t.lower() in low]
    return out


def scrub_tree(root: Path, org: Org | None = None) -> list[str]:
    """Scan every text file under ``root`` (skipping .git) - used by pre-push hooks and releases."""
    terms = [t for t in ((org.private_terms + ([org.name] if org.name else [])) if org else []) if t]
    findings = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or ".git" in f.relative_to(root).parts or f.suffix not in TEXT_SUFFIXES:
            continue
        text = f.read_text(errors="replace")
        if f.name == "sop.json":
            try:
                d = json.loads(text)
                d.pop("origin", None)
                text = json.dumps(d)
            except json.JSONDecodeError:
                pass
        findings += scrub_text(text, terms, str(f.relative_to(root)))
    return findings + external_scan(root)


def external_scan(root: Path) -> list[str]:
    """Run gitleaks / trufflehog when installed (a second, independent secret scanner)."""
    out = []
    if shutil.which("gitleaks"):
        p = subprocess.run(["gitleaks", "detect", "--no-git", "--no-banner", "--redact", "--source", str(root),
                            "--report-format", "json", "--report-path", "/dev/stdout"], capture_output=True, text=True)
        if p.returncode == 1:
            try:
                out += [f"{x.get('File')}: gitleaks: {x.get('RuleID')}" for x in json.loads(p.stdout or "[]")]
            except json.JSONDecodeError:
                out.append("gitleaks: findings (unparsed)")
    if shutil.which("trufflehog"):
        p = subprocess.run(["trufflehog", "filesystem", str(root), "--json", "--no-update", "--fail"],
                           capture_output=True, text=True)
        if p.returncode == 183:
            out.append("trufflehog: verified or unverified secrets found")
    return out


def scrub(sop: SOP, org: Org) -> list[str]:
    return scrub_tree(sop.path, org)


PERSONAL_CUES = ("company internal our team customer account specific hostname credentials proprietary private "
                 "business rule employee tenant workspace personal my database production endpoint")
GENERAL_CUES = ("generic standard reusable common http json csv yaml file git test parse format convert extract "
                "summarize library utility any project open source")


def generality(jev: Jev, sop: SOP) -> float:
    return jev.yes("Is this procedure generic, useful to anyone, not specific to one organization?", sop.text,
                   GENERAL_CUES, PERSONAL_CUES)


def classify(jev: Jev, sop: SOP, org: Org, save: bool = True) -> dict:
    """Personal (stays private) or general (may be proposed for the public registry).

    Deterministic signals first - any scrubber finding or private term makes it personal.
    Otherwise the JEV decides, and it must be clearly confident to call something general:
    anything uncertain stays private.
    """
    findings = scrub(sop, org)
    body = ""
    for f in sorted(sop.path.glob("*")):
        if f.is_file() and f.suffix in (".py", ".sh", ".md"):
            body += f.read_text(errors="replace")[:1500]
    d = jev.choose("Is this procedure personal to one user or organization, or general enough to share publicly?",
                   f"{sop.text} {' '.join(org.private_terms)} {body[:2000]}",
                   [Option("personal", PERSONAL_CUES), Option("general", GENERAL_CUES)])
    if findings:
        vis, reason = "private", f"scrubber: {findings[0]}" + (f" (+{len(findings) - 1} more)" if len(findings) > 1 else "")
    elif d.probs["general"] >= 0.65 and d.probs["general"] - d.probs["personal"] >= 0.15:
        vis, reason = "shareable", "JEV: general"
    else:
        vis, reason = "private", "JEV: personal or not clearly general"
    result = {"visibility": vis, "reason": reason, "probs": d.probs, "decision": d.id, "findings": len(findings)}
    if save and sop.scope == "private":
        data = json.loads((sop.path / "sop.json").read_text())
        data["visibility"] = vis
        data["classified"] = {k: result[k] for k in ("reason", "probs")}
        (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
    return result


def publish(sop: SOP, dest_pkg: Path, org: Org, force: bool = False) -> Path:
    findings = scrub(sop, org)
    secrets = [f for f in findings if any(f": {k}:" in f for k in SECRET_KINDS) or "gitleaks" in f or "trufflehog" in f]
    if secrets:
        raise PermissionError("secrets found (cannot be overridden):\n  " + "\n  ".join(secrets))
    if findings and not force:
        raise PermissionError("scrubber findings:\n  " + "\n  ".join(findings))
    target = dest_pkg.joinpath(*sop.id.split("."))
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(sop.path, target, ignore=shutil.ignore_patterns("__pycache__"))
    d = json.loads((target / "sop.json").read_text())
    for k in ("origin", "classified", "visibility"):
        d.pop(k, None)
    d["scope"] = "public"
    (target / "sop.json").write_text(json.dumps(d, indent=2) + "\n")
    return target


def build_index(pkg_root: Path) -> Path:
    lib = Library([(pkg_root, "public")])
    entries = [{"id": s.id, "description": s.description, "keywords": s.keywords, "kind": s.kind,
                "permissions": s.permissions, "version": s.version, "inputs": s.inputs,
                "path": str(s.path.relative_to(pkg_root))} for s in lib.sops.values()]
    p = pkg_root / "index.json"
    p.write_text(json.dumps({"format": 1, "sops": entries}, indent=1))
    return p


def search_index(jev: Jev, index_src: str, query: str, k: int = 10) -> list[tuple[dict, float]]:
    if re.match(r"https?://", index_src):
        with urllib.request.urlopen(index_src, timeout=15) as r:
            idx = json.loads(r.read())
    else:
        idx = json.loads(Path(index_src).read_text())
    entries = idx["sops"]
    d = jev.activate("Which registry procedures match this need?", query,
                     [Option(e["id"], f"{e['id']} {e['description']} {' '.join(e.get('keywords', []))}") for e in entries])
    by_id = {e["id"]: e for e in entries}
    return [(by_id[i], p) for i, p in d.top(k) if p > 0.1]


def install(src: str, home: Path, name: str | None = None) -> Path:
    """Install a package from a local directory or a git URL into ~/.rameness/public/<name>."""
    dest_root = home / "public"
    dest_root.mkdir(parents=True, exist_ok=True)
    name = name or re.sub(r"\.git$", "", src.rstrip("/").split("/")[-1])
    dest = dest_root / name
    if dest.exists():
        shutil.rmtree(dest)
    if re.match(r"(https?|git|ssh)://|git@", src):
        subprocess.run(["git", "clone", "--depth", "1", src, str(dest)], check=True)
        shutil.rmtree(dest / ".git", ignore_errors=True)
    else:
        shutil.copytree(src, dest)
    return dest
