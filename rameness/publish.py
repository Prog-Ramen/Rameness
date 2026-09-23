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
    "secret": re.compile(r"(?i)(api[_-]?key|secret|token|passw(or)?d|bearer)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}"),
    "key-like": re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{10,})"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "private-ip": re.compile(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b"),
    "private-host": re.compile(r"\b[\w-]+(\.[\w-]+)*\.(internal|local|corp|lan|intranet)\b"),
    "home-path": re.compile(r"(/home/[\w.-]+|/Users/[\w.-]+|C:\\\\Users\\\\)"),
}


def scrub(sop: SOP, org: Org) -> list[str]:
    findings = []
    files = [p for p in sop.path.rglob("*") if p.is_file() and p.suffix in (".py", ".sh", ".json", ".md", ".txt", "")]
    terms = [t for t in org.private_terms + ([org.name] if org.name else []) if t]
    for f in files:
        text = f.read_text(errors="replace")
        if f.name == "sop.json":   # origin metadata is stripped on publish; don't scan it
            d = json.loads(text)
            d.pop("origin", None)
            text = json.dumps(d)
        for kind, rx in PATTERNS.items():
            for m in rx.finditer(text):
                findings.append(f"{f.name}: {kind}: {m.group(0)[:60]}")
        for t in terms:
            if t.lower() in text.lower():
                findings.append(f"{f.name}: private term: {t}")
    return findings


def generality(jev: Jev, sop: SOP) -> float:
    return jev.yes("Is this procedure generic, useful to anyone, not specific to one organization?", sop.text,
                   "generic standard common reusable http json csv file git test parse convert format",
                   "company internal proprietary customer specific acme team our")


def publish(sop: SOP, dest_pkg: Path, org: Org, force: bool = False) -> Path:
    findings = scrub(sop, org)
    if findings and not force:
        raise PermissionError("scrubber findings:\n  " + "\n  ".join(findings))
    target = dest_pkg.joinpath(*sop.id.split("."))
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(sop.path, target, ignore=shutil.ignore_patterns("__pycache__"))
    d = json.loads((target / "sop.json").read_text())
    d.pop("origin", None)
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
