"""Public SOP ecosystem: scrub, publish, index, search, install.

Private SOPs never leave the machine on their own. Scrubber findings come in two classes:

* hard: secrets (keys, tokens, private keys, JWTs, credentials in URLs, gitleaks/trufflehog hits)
  and the organization's own ``private_terms``. These always block.
* soft: pattern hits (emails, private hosts/IPs, home paths) that are often harmless
  (``user@example.com``, ``192.168.0.1`` in a docstring). JEV decides whether they are
  private info that could cause issues if published.

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


def is_hard(finding: str) -> bool:
    return (any(f": {k}:" in finding for k in SECRET_KINDS) or ": private term:" in finding
            or "gitleaks" in finding or "trufflehog" in finding)


def hard_findings(findings: list[str]) -> list[str]:
    return [f for f in findings if is_hard(f)]


LEAK_CUES = ("real internal hostname company employee customer personal email address production server database "
             "private network credentials user home directory account tenant proprietary")
BENIGN_CUES = ("example placeholder localhost test fixture sample documentation dummy default generic "
               "example.com example.org noreply 127.0.0.1 192.168.0.1 docs tutorial")
LEAK_Q = "Do these scrubber findings expose private information that could cause issues if published?"


def private_info(jev: Jev, sop: SOP, findings: list[str]) -> dict:
    """JEV's call on soft findings: {'leak': bool, 'p': P(leak), 'findings': [...]}.

    Hard findings are not weighed here - they always block. With no soft findings there is
    nothing to decide.
    """
    soft = [f for f in findings if not is_hard(f)]
    if not soft:
        return {"leak": False, "p": 0.0, "findings": []}
    lines = []                                   # the line each match sits on: "example: user@example.com" vs a real login
    for f in soft[:20]:
        path, _, match = f.split(": ", 2)
        src = sop.path / path
        text = src.read_text(errors="replace") if src.is_file() else ""
        lines += [ln.strip()[:200] for ln in text.splitlines() if match[:60] in ln][:2] or [match]
    d = jev.choose(LEAK_Q, " ; ".join(lines),
                   [Option("leak", LEAK_CUES, desc="Real private information: a real person's email, an internal "
                                                  "hostname or IP, a user's home directory, or company-specific data."),
                    Option("benign", BENIGN_CUES, desc="Harmless: example or placeholder values, documentation, "
                                                      "test fixtures, localhost or standard default addresses.")])
    return {"leak": d.probs["leak"] >= d.probs["benign"], "p": d.probs["leak"], "findings": soft}


PERSONAL_CUES = ("company internal our team customer account specific hostname credentials proprietary private "
                 "business rule employee tenant workspace personal my database production endpoint")
GENERAL_CUES = ("generic standard reusable common http json csv yaml file git test parse format convert extract "
                "summarize library utility any project open source")


def generality(jev: Jev, sop: SOP) -> float:
    return jev.yes("Is this procedure generic, useful to anyone, not specific to one organization?", sop.digest(),
                   GENERAL_CUES, PERSONAL_CUES,
                   yes_desc="Yes: generic and useful to anyone.", no_desc="No: specific to one organization.")


def classify(jev: Jev, sop: SOP, org: Org, save: bool = True) -> dict:
    """Personal (stays private) or general (may be proposed for the public registry).

    Hard scrubber findings (secrets, private terms) make it private. JEV judges the soft
    findings, and if it sees no leak it decides personal vs general; it must be clearly
    confident to call something general: anything uncertain stays private.
    """
    findings = scrub(sop, org)
    hard = hard_findings(findings)
    leak = private_info(jev, sop, findings) if not hard else {"leak": False, "findings": []}
    d = jev.choose("Is this procedure personal to one user or organization, or general enough to share publicly?",
                   sop.digest(),                     # the SOP's own code; private terms are checked by the scrubber
                   [Option("personal", PERSONAL_CUES, desc="Specific to one user or organization: its own systems, "
                                                          "data, customers, accounts or business rules."),
                    Option("general", GENERAL_CUES, desc="A generic, reusable procedure useful to anyone, with nothing "
                                                        "organization-specific in it.")])
    if hard:
        vis, reason = "private", f"scrubber: {hard[0]}" + (f" (+{len(hard) - 1} more)" if len(hard) > 1 else "")
    elif leak["leak"]:
        vis, reason = "private", f"JEV: private info in {leak['findings'][0]}"
    elif d.probs["general"] >= 0.65 and d.probs["general"] - d.probs["personal"] >= 0.15:
        vis, reason = "shareable", "JEV: general"
    else:
        vis, reason = "private", "JEV: personal or not clearly general"
    result = {"visibility": vis, "reason": reason, "probs": d.probs, "decision": d.id, "findings": len(findings)}
    if leak["findings"] and not leak["leak"]:
        result["reason"] += f" ({len(leak['findings'])} scrubber finding(s) judged benign)"
    if save and sop.scope == "private":
        data = json.loads((sop.path / "sop.json").read_text())
        data["visibility"] = vis
        data["classified"] = {k: result[k] for k in ("reason", "probs")}
        (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
    return result


def categorize(jev: Jev, lib: Library, sop: SOP, min_p: float = 0.5) -> str:
    """The top-level category an SOP belongs in, chosen by JEV from the library's categories.

    The generator proposes ``category.name``; JEV checks it against the categories that exist (with
    their descriptions). A proposed category that matches none of them is kept as a new one, unless
    JEV clearly places the SOP in an existing category. Returns the category id."""
    proposed = sop.id.split(".")[0] if "." in sop.id else ""
    cats = {cid: node for cid, node in lib.root.children.items() if node.sop is None and cid != "learned"}
    if not cats:
        return proposed or "learned"
    opts = [Option(cid, node.text, desc=node.desc) for cid, node in cats.items()]
    if proposed and proposed not in cats and proposed != "learned":
        opts.append(Option(proposed, f"{proposed} new category {sop.text}",
                           desc=f"A new category, '{proposed}', for procedures like this one"))
    d = jev.choose("Which category of standard procedures does this procedure belong in?",
                   f"{sop.id}: {sop.description}", opts)
    best = max(d.probs, key=d.probs.get)
    if d.probs[best] >= min_p or not proposed or proposed == "learned":
        return best
    return proposed


def recategorize(lib: Library, sop: SOP, category: str) -> SOP:
    """Move a private SOP to ``<category>.<name>`` (its folder and id), keeping everything else."""
    name = sop.id.split(".")[-1]
    new_id = f"{category}.{name}"
    if new_id == sop.id or sop.scope != "private":
        return sop
    root = sop.path
    for _ in sop.id.split("."):
        root = root.parent
    target = root.joinpath(*new_id.split("."))
    if target.exists():
        return sop
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(sop.path), str(target))
    data = json.loads((target / "sop.json").read_text())
    data["id"] = new_id
    (target / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
    lib.reload()
    return lib.get(new_id)


BUILTIN_CUES = ("common everyday basic core almost every task most runs file text json http git test syntax "
                "search read check format")
REGISTRY_CUES = ("specialised niche particular domain tool service format occasional some tasks optional")


def destination(jev: Jev, sop: SOP, runs_seen: int, runs_total: int, min_share: float = 0.5,
                min_runs: int = 10) -> dict:
    """Where a shareable SOP is proposed: ``builtin`` (shipped with Rameness, no network pull) or
    ``registry`` (RamenSOPs, pulled when a task needs it).

    Built-in is for procedures used in most runs. The evidence is the share of successful runs the
    procedure appeared in; it must reach ``min_share`` over at least ``min_runs`` runs, and JEV must
    also judge it broadly needed. Anything else goes to the registry, which costs only a pull."""
    share = runs_seen / runs_total if runs_total else 0.0
    d = jev.choose("Should this procedure ship with the agent itself because almost every task uses it, or live "
                   "in the public registry for the tasks that need it?",
                   f"{sop.id}: {sop.description}. Used in {runs_seen} of {runs_total} recent successful runs.",
                   [Option("builtin", BUILTIN_CUES, desc="A basic procedure almost every task uses; worth shipping "
                                                         "with the agent so it never has to be downloaded."),
                    Option("registry", REGISTRY_CUES, desc="Useful for some tasks; fetched from the registry when "
                                                           "a task needs it.")])
    if runs_total < min_runs:
        dest, reason = "registry", f"only {runs_total} runs of evidence (built-in needs {min_runs})"
    elif share < min_share:
        dest, reason = "registry", f"used in {share:.0%} of runs (built-in needs {min_share:.0%})"
    elif d.probs["builtin"] >= 0.6 and d.probs["builtin"] - d.probs["registry"] >= 0.15:
        dest, reason = "builtin", f"used in {share:.0%} of runs; JEV: broadly needed"
    else:
        dest, reason = "registry", f"used in {share:.0%} of runs, but JEV not confident it is broadly needed"
    return {"destination": dest, "reason": reason, "share": round(share, 3), "probs": d.probs}

def publish(sop: SOP, dest_pkg: Path, org: Org, force: bool = False) -> Path:
    findings = scrub(sop, org)
    secrets = [f for f in findings if is_hard(f) and ": private term:" not in f]
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
    """Sharded, metadata-only index for lazy traversal.

    ``index.json`` lists only the top-level entries; every category directory gets an
    ``_index.json`` listing only its own children. A client fetches the listing of a category
    only when JEV decides to explore it, and downloads code only for the SOPs it selects
    (each file carries a SHA-256 so it can be verified before it runs).
    """
    import hashlib
    lib = Library([(pkg_root, "public")])

    def entry(n) -> dict:
        if n.sop:
            s = n.sop
            files = [{"path": str(f.relative_to(pkg_root)), "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
                     for f in sorted(s.path.rglob("*")) if f.is_file() and "__pycache__" not in f.parts]
            return {"type": "sop", "id": s.id, "description": s.description, "keywords": s.keywords, "kind": s.kind,
                    "permissions": s.permissions, "version": s.version, "inputs": s.inputs, "status": s.status,
                    "path": str(s.path.relative_to(pkg_root)), "files": files}
        return {"type": "node", "id": n.id, "description": n.description, "keywords": n.keywords,
                "requires": [{"name": r.name, "hints": r.hints, "question": r.question} for r in n.requires],
                "children": sorted(n.children)}

    for n in lib.root.walk():
        if n.id and not n.sop:
            d = pkg_root.joinpath(*n.id.split("."))
            (d / "_index.json").write_text(json.dumps(
                {"format": 3, "id": n.id, "entries": [entry(c) for _, c in sorted(n.children.items())]}, indent=1))
    p = pkg_root / "index.json"
    p.write_text(json.dumps({"format": 3, "id": "", "entries": [entry(c) for _, c in sorted(lib.root.children.items())]},
                            indent=1))
    return p


def search_index(jev: Jev, index_src: str, query: str, k: int = 10) -> list[tuple[dict, float]]:
    """Search a registry by lazy traversal: only the branches JEV explores are fetched."""
    from .remote import RemoteRegistry
    reg = RemoteRegistry(index_src, Path.home() / ".rameness", ttl=0)
    hits = reg.traverse(jev, query, "", {}, set(), activate_th=0.1, explore_th=0.15, beam=6, max_sops=k)
    return [(reg.entries[s.id], p) for s, p in hits]


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
