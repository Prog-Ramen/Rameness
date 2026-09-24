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


SPECIFIC_Q = ("Which of these details are specific to one organization (its business rules, thresholds, "
              "plans, internal names, identifiers) rather than generic defaults anyone would use?")
SHARE_Q = "Is this SOP general enough to share publicly, or does it encode one organization's use case?"
SHARE_OPTIONS = [
    Option("shareable", "generic reusable technique any organization could use standard tooling no internal details"),
    Option("generalize", "useful technique but hard-codes one organization's constants thresholds names that should "
                         "become parameters before sharing"),
    Option("private", "one organization's business process internal systems policies customers people workflow"),
]

_DETAIL_PATTERNS = [
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|(?:percent|days?|hours?|minutes?|weeks?|months?|years?)\b)", re.I),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*=\s*[^\n]{1,60}"),                          # UPPER_CASE = constant
    re.compile(r"\b(?:GL|SKU|acct|account|tier|plan|region|cluster|env)\s*[-#:]?\s*[A-Za-z0-9][\w-]{0,15}\b", re.I),
    re.compile(r"(?<![\w/])[#@][a-z][\w-]{2,}"),                                      # #channel / @team
    re.compile(r"\b[a-z][\w-]*/[\w.-]+(?:/[\w.-]+)*"),                              # repo/path-like names
    re.compile(r"(?<=[a-z,;:] )[A-Z][a-zA-Z0-9]{1,}(?:[A-Z][a-z0-9]+)*\b"),           # mid-sentence proper nouns
    re.compile(r"\bQ[1-4]\b|\b(?:FY|CY)\d{2,4}\b"),
]


def extract_details(sop: SOP, limit: int = 24) -> list[dict]:
    """Candidate specifics for JEV to judge. Deliberately over-inclusive: JEV decides, not the regex."""
    texts = [("description", sop.description)]
    for f in sorted(sop.path.glob("*")):
        if f.is_file() and f.suffix in (".py", ".sh", ".md", ".json") and f.name != "sop.json":
            texts.append((f.name, f.read_text(errors="replace")[:6000]))
    seen, out = set(), []
    for label, text in texts:
        for line in text.splitlines():
            for rx in _DETAIL_PATTERNS:
                for m in rx.finditer(line):
                    t = m.group(0).strip()
                    if t.lower() in seen or len(t) < 2:
                        continue
                    seen.add(t.lower())
                    out.append({"text": t, "where": label, "context": line.strip()[:160]})
    return out[:limit]


def strong_jev(jev: Jev) -> Jev | None:
    """The backend allowed to make high-stakes calls: a served JEV or a model, never the keyword pass alone."""
    from .jev import CascadeJev, HttpJev, LLMJev
    b = jev.backend
    if isinstance(b, (LLMJev, HttpJev)):
        return jev
    if isinstance(b, CascadeJev) and b.strong is not None:
        return Jev(b.strong, jev.log_path, tuning_path=jev.tuning_path)
    return None


def classify(jev: Jev, sop: SOP, org: Org, save: bool = True, confident: float = 0.85) -> dict:
    """Shareable (may be proposed) or private. JEV decides; code only gathers evidence.

    1. The scrubber (secrets, private hosts/IPs, emails, home paths, your private_terms) makes an
       SOP private outright - no model needed.
    2. Candidate details (numbers with units, UPPER_CASE constants, plan/tier/account codes,
       #channels, repo paths, proper nouns...) are extracted, and JEV decides which of them are
       specific to one organization.
    3. JEV decides shareable / generalize / private with those findings in view.

    Outcomes:
      shareable - JEV is highly confident (>= ``confident``) it is general and flagged nothing
      ambiguous - JEV is not highly certain it isn't a personal use case (lower confidence,
                  details it couldn't call either way, or no model-backed JEV): the contributor
                  signs off after seeing the files and JEV's evidence
      private   - JEV is confident it is one organization's use case, or it hard-codes
                  organization-specific details (then the reason lists what to parameterize)
    Only a strong JEV (served model or LLM) may call something shareable.
    """
    findings = scrub(sop, org)
    body = "".join(f.read_text(errors="replace")[:1500] for f in sorted(sop.path.glob("*"))
                   if f.is_file() and f.suffix in (".py", ".sh", ".md"))
    result: dict = {"visibility": "private", "specific": [], "uncertain": [], "unclassified": False, "probs": {},
                    "decision": None, "findings": len(findings)}
    decider = strong_jev(jev)
    if findings:
        result["reason"] = f"scrubber: {findings[0]}" + (f" (+{len(findings) - 1} more)" if len(findings) > 1 else "")
    elif decider is None:
        result.update(visibility="ambiguous", unclassified=True,
                      reason="no model-backed JEV available: needs your sign-off")
    else:
        details = extract_details(sop)
        if details:
            d1 = decider.activate(SPECIFIC_Q, f"{sop.id}: {sop.description}",
                                  [Option(f"d{i}", f"{x['text']}  (in {x['where']}: {x['context']})")
                                   for i, x in enumerate(details)])
            ranked = [(details[int(k[1:])]["text"], p) for k, p in d1.top(len(details))]
            result["specific"] = [t for t, p in ranked if p >= 0.7]
            result["uncertain"] = [t for t, p in ranked if 0.3 <= p < 0.7]
        q = (f"{sop.id}: {sop.description}\ncode:\n{body[:1800]}\norganization-specific details found: "
             f"{', '.join(result['specific']) or 'none'}")
        d2 = decider.choose(SHARE_Q, q, SHARE_OPTIONS)
        result.update(probs=d2.probs, decision=d2.id)
        ps = d2.probs
        if result["specific"] or (d2.best == "generalize" and ps["generalize"] >= 0.6):
            result["reason"] = ("JEV: generalize first - make these parameters: " + ", ".join(result["specific"])
                                if result["specific"] else "JEV: generalize first")
        elif d2.best == "private" and ps["private"] >= 0.6:
            result["reason"] = "JEV: one organization's use case"
        elif d2.best == "shareable" and ps["shareable"] >= confident and not result["uncertain"]:
            result.update(visibility="shareable", reason=f"JEV: general ({ps['shareable']:.0%})")
        else:
            doubts = [f"shareable only {ps['shareable']:.0%}"] if ps["shareable"] < confident else []
            if result["uncertain"]:
                doubts.append("unsure about " + ", ".join(result["uncertain"]))
            if d2.best != "shareable":
                doubts.append(f"leans {d2.best}")
            result.update(visibility="ambiguous", reason="JEV is not certain: " + "; ".join(doubts))
    if save and sop.scope == "private":
        data = json.loads((sop.path / "sop.json").read_text())
        data["visibility"] = result["visibility"]
        data["classified"] = {k: result[k] for k in ("reason", "probs", "specific", "uncertain", "unclassified")}
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
    from .config import user_home
    if not re.match(r"(https?|file)://", index_src):
        index_src = Path(index_src).resolve().as_uri()           # local index.json path
    reg = RemoteRegistry(index_src, user_home(), ttl=0)
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
