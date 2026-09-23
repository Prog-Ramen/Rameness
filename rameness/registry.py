"""Two-repo SOP registry: review in private, release in public.

A pull request to a public GitHub repo is public the moment its branch is pushed, and
forks of public repos are public too. So proposed SOPs are reviewed in a separate
**private staging repo**, and only reviewed, merged, re-scanned SOPs are **released**
to the public registry::

    private library ──propose──► private staging repo (PR review) ──merge──► release ──► public repo
       (JEV: personal/general,       verified not public before              re-scan,
        scrubber, secret scan)       every push; pre-push scan hook          metadata index

Guards, all deterministic (JEV confidence never overrides them):

* only SOPs JEV classified ``shareable`` can be proposed (personal needs an explicit override,
  and still has to pass every scan)
* scrubber + gitleaks/trufflehog (when installed) must be clean - secrets cannot be overridden
* the staging remote must be verifiably not public before anything is pushed
* every clone rameness manages gets a pre-push hook that re-scans the whole tree
* release re-scans the merged staging content before it reaches the public repo

Config (``.rameness/config.json`` or ``~/.rameness/config.json``)::

    "registry": {"staging": "git@github.com:Org/sops-staging.git",   # private
                 "public":  "git@github.com:Org/sops.git",
                 "release": "pr"}                                     # pr | direct
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .org import Org
from .publish import SECRET_KINDS, build_index, classify, scrub, scrub_tree
from .sops import SOP, Library

GH_URL = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


class RegistryError(Exception):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-c", "user.name=rameness", "-c", "user.email=rameness@localhost", *args],
                       cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise RegistryError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def visibility(url: str, assume_private: bool = False) -> tuple[bool, str]:
    """(is_not_public, how we know). Refuses to guess in the unsafe direction."""
    if url.startswith(("/", "./", "../", "file://")) or Path(url).exists():
        return True, "local repository"
    m = GH_URL.search(url)
    if not m:
        return (True, "assumed private by config") if assume_private else \
               (False, "not a GitHub URL: cannot verify it is private (set registry.staging_assume_private)")
    owner, repo = m.groups()
    if shutil.which("gh"):
        p = subprocess.run(["gh", "repo", "view", f"{owner}/{repo}", "--json", "visibility", "-q", ".visibility"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            vis = p.stdout.strip().upper()
            return vis in ("PRIVATE", "INTERNAL"), f"gh: {vis.lower()}"
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}",
                                 headers={"Accept": "application/vnd.github+json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if token:
            return bool(data.get("private")), f"GitHub API: {'private' if data.get('private') else 'public'}"
        return False, "GitHub API: visible without credentials, so it is public"
    except urllib.error.HTTPError as e:
        if e.code == 404 and not token:
            return True, "GitHub API: not visible without credentials (private or nonexistent)"
        return False, f"GitHub API error {e.code}: cannot verify"
    except Exception as e:
        return False, f"cannot verify visibility ({type(e).__name__})"


def install_hook(repo: Path) -> None:
    """pre-push: re-scan the whole tree; any finding blocks the push."""
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n# installed by rameness: blocks pushes containing secrets or private data\n"
                    f"exec \"{sys.executable}\" -m rameness sop scrub-tree \"$(git rev-parse --show-toplevel)\"\n")
    hook.chmod(0o755)


def sanitized_copy(sop: SOP, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sop.path, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*"))
    d = json.loads((dest / "sop.json").read_text())
    for k in ("origin", "classified", "visibility", "stats"):
        d.pop(k, None)
    d["scope"] = "public"
    (dest / "sop.json").write_text(json.dumps(d, indent=2) + "\n")


class Registry:
    def __init__(self, cfg: dict, home: Path, org: Org, jev):
        rc = cfg.get("registry") or {}
        self.staging = rc.get("staging")
        self.public = rc.get("public")
        self.release_mode = rc.get("release", "pr")
        self.assume_private = bool(rc.get("staging_assume_private"))
        self.home = home / "registry"
        self.org, self.jev = org, jev
        self.ledger = self.home / "proposals.json"

    # ---- helpers

    def _clone(self, url: str, name: str) -> Path:
        d = self.home / name
        if not (d / ".git").exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            p = subprocess.run(["git", "clone", "-q", url, str(d)], capture_output=True, text=True)
            if p.returncode:
                raise RegistryError(f"clone {url}: {p.stderr.strip()}")
        else:
            git(d, "remote", "set-url", "origin", url)
        git(d, "fetch", "-q", "origin")
        heads = git(d, "branch", "-r", check=False)
        self._base = "main" if "origin/main" in heads else ("master" if "origin/master" in heads else None)
        if self._base:
            git(d, "checkout", "-q", "-B", self._base, f"origin/{self._base}")
        else:                                    # empty repo
            self._base = "main"
            git(d, "checkout", "-q", "--orphan", "main", check=False)
        git(d, "clean", "-qfdx")
        install_hook(d)
        return d

    def _gh_pr(self, url: str, branch: str, base: str, title: str, body: str) -> str | None:
        m = GH_URL.search(url)
        if not (m and shutil.which("gh")):
            return None
        p = subprocess.run(["gh", "pr", "create", "--repo", f"{m.group(1)}/{m.group(2)}", "--head", branch,
                            "--base", base, "--title", title, "--body", body], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    def _compare_url(self, url: str, branch: str, base: str) -> str | None:
        m = GH_URL.search(url)
        return f"https://github.com/{m.group(1)}/{m.group(2)}/compare/{base}...{branch}?expand=1" if m else None

    def proposals(self) -> list[dict]:
        return json.loads(self.ledger.read_text()) if self.ledger.exists() else []

    def _record(self, entry: dict) -> None:
        allp = self.proposals() + [entry]
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(allp, indent=1))

    # ---- propose (private library -> private staging PR)

    def propose(self, sop: SOP, override_personal: bool = False, reclassify: bool = True) -> dict:
        if not self.staging:
            raise RegistryError("no staging repo configured (registry.staging) - it must be a PRIVATE repo")
        if sop.scope != "private":
            raise RegistryError(f"{sop.id} is already public")
        if sop.status != "validated":
            raise RegistryError(f"{sop.id} is {sop.status}: only SOPs whose tests pass can be proposed")
        cls = classify(self.jev, sop, self.org) if reclassify else {"visibility": sop.visibility, "reason": "stored"}
        findings = scrub(sop, self.org)
        secrets = [f for f in findings if any(f": {k}:" in f for k in SECRET_KINDS) or "gitleaks" in f or "trufflehog" in f]
        if secrets:
            raise RegistryError("secrets found - never proposable:\n  " + "\n  ".join(secrets))
        if findings:
            raise RegistryError("private data found - remove it first:\n  " + "\n  ".join(findings))
        if cls["visibility"] != "shareable" and not override_personal:
            raise RegistryError(f"JEV classified {sop.id} as personal ({cls['reason']}); it stays private. "
                                "If you are sure it is general, pass --override-personal (scans still apply).")
        ok, how = visibility(self.staging, self.assume_private)
        if not ok:
            raise RegistryError(f"refusing to push: staging repo is not verifiably private ({how})")
        repo = self._clone(self.staging, "staging")
        branch = f"sop/{sop.id}-{time.strftime('%Y%m%d%H%M%S')}"
        git(repo, "checkout", "-q", "-b", branch)
        sanitized_copy(sop, repo / "sops" / Path(*sop.id.split(".")))
        build_index(repo / "sops")
        leftover = scrub_tree(repo, self.org)
        if leftover:
            raise RegistryError("scan of the staging tree failed:\n  " + "\n  ".join(leftover))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"Propose SOP {sop.id}\n\n{sop.description}")
        git(repo, "push", "-q", "-u", "origin", branch)
        body = (f"Proposed SOP `{sop.id}`: {sop.description}\n\nClassification: {cls['visibility']} ({cls['reason']}).\n"
                "Scans: rameness scrubber" + (" + gitleaks" if shutil.which("gitleaks") else "") +
                (" + trufflehog" if shutil.which("trufflehog") else "") + ": clean.\n"
                "Merging here does not publish anything; `rameness sop release` does, after re-scanning.")
        pr = self._gh_pr(self.staging, branch, self._base, f"SOP: {sop.id}", body)
        entry = {"id": sop.id, "branch": branch, "t": time.time(), "staging": self.staging, "where": how,
                 "pr": pr or self._compare_url(self.staging, branch, self._base), "classification": cls["visibility"],
                 "overridden": cls["visibility"] != "shareable"}
        self._record(entry)
        return entry

    # ---- release (merged staging -> public)

    def release(self) -> dict:
        if not (self.staging and self.public):
            raise RegistryError("registry.staging and registry.public must both be configured")
        ok, how = visibility(self.staging, self.assume_private)
        if not ok:
            raise RegistryError(f"staging repo is not verifiably private ({how}); refusing to treat it as a review queue")
        st = self._clone(self.staging, "staging")
        src = st / "sops"
        if not src.exists():
            return {"released": [], "note": "nothing merged in staging yet"}
        findings = scrub_tree(src, self.org)
        if findings:
            raise RegistryError("merged staging content failed the scan; nothing released:\n  " + "\n  ".join(findings))
        pub = self._clone(self.public, "public")
        base = self._base
        branch = base if self.release_mode == "direct" else f"release/{time.strftime('%Y%m%d-%H%M%S')}"
        dest = pub / "sops"
        changed = []
        for sj in sorted(src.rglob("sop.json")):
            rel = sj.parent.relative_to(src)
            target = dest / rel
            new = {f.relative_to(sj.parent): f.read_bytes() for f in sj.parent.rglob("*") if f.is_file()}
            old = {f.relative_to(target): f.read_bytes() for f in target.rglob("*") if f.is_file()} if target.exists() else {}
            if new != old:
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(sj.parent, target)
                changed.append(".".join(rel.parts))
        for nj in sorted(src.rglob("_node.json")):       # category metadata travels with its SOPs
            target = dest / nj.relative_to(src)
            if not target.exists() or target.read_bytes() != nj.read_bytes():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(nj, target)
                changed.append(".".join(nj.parent.relative_to(src).parts) + "/")
        if not changed:
            return {"released": [], "note": "public registry already up to date"}
        build_index(dest)
        git(pub, "add", "-A")
        pending = sorted(b.strip() for b in git(pub, "branch", "-r", "--list", "origin/release/*", check=False).splitlines())
        if pending and branch != base and subprocess.run(
                ["git", "diff", "--quiet", "--cached", pending[-1], "--", "sops"], cwd=pub).returncode == 0:
            git(pub, "reset", "-q", "--hard")
            return {"released": [], "note": f"identical release already pending review: {pending[-1]}"}
        if branch != base:
            git(pub, "checkout", "-q", "-B", branch)          # carries the staged release onto its branch
        git(pub, "commit", "-q", "-m", "Release SOPs: " + ", ".join(changed))
        git(pub, "push", "-q", "-u", "origin", branch)
        pr = None
        if branch != base:
            pr = self._gh_pr(self.public, branch, base, f"Release {len(changed)} SOP(s)",
                             "Reviewed in the private staging registry and re-scanned before release:\n"
                             + "\n".join(f"- `{c}`" for c in changed)) or self._compare_url(self.public, branch, base)
        return {"released": changed, "branch": branch, "pr": pr}


PUBLIC_README = """# RamenSOPs

The public registry of general-purpose **SOPs** (standard operating procedures) for
[Rameness](https://github.com/Prog-Ramen/Rameness): the JEV-driven agent harness.

An SOP is a tested, parameterised procedure an agent can run instead of re-deriving the same
steps. Rameness learns SOPs from repeated work, keeps them private by default, and only
general ones - reviewed privately, scanned for secrets - are released here.

## Layout

```
sops/
  index.json                 metadata-only index (discovery never downloads code)
  <category>/_node.json      category description, keywords, requirements
  <category>/<name>/sop.json interface: description, inputs (JSON schema), permissions, tests
  <category>/<name>/run.py   implementation: JSON args on stdin -> JSON result on stdout
```

## Use

```bash
rameness sop install https://github.com/Prog-Ramen/RamenSOPs
rameness sop remote "fetch json from an api"      # searches sops/index.json
rameness sop test                                  # runs every SOP's embedded tests
```

## Contribute

SOPs are not proposed here directly: a PR to a public repo is public as soon as it is pushed.
Rameness proposes SOPs to a private staging registry for review (`rameness sop propose`), then
`rameness sop release` re-scans merged SOPs and opens a release PR here. Every SOP must be
general (no organization-specific endpoints, names or data), contain no secrets, and pass its tests.
"""

STAGING_README = """# SOP staging registry (PRIVATE)

Keep this repository **private**. SOPs proposed with `rameness sop propose` land here as pull
requests for review; nothing here is public. After merging, `rameness sop release` re-scans the
merged SOPs and publishes them to the public registry.
"""

SCAN_WORKFLOW = """name: secret-scan
on: [pull_request, push]
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: gitleaks/gitleaks-action@v2
        env: {GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}"}
"""


def seed_public(url: str, home: Path, org: Org, sources: list[Path], push: bool = True) -> dict:
    """First population of an (empty) public registry from already-public SOP packages.

    ``main`` gets the README and a secret-scan workflow; the SOPs go on a ``seed/...`` branch for
    a review PR. Same guards as a release: sanitized copies, full-tree scan, pre-push hook.
    """
    d = home / "registry" / "public-seed"
    if d.exists():
        shutil.rmtree(d)
    d.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["git", "clone", "-q", url, str(d)], capture_output=True, text=True)
    if p.returncode:
        raise RegistryError(f"clone {url}: {p.stderr.strip()}")
    install_hook(d)
    empty = not git(d, "branch", "-r", check=False).strip()
    base = "main"
    if empty:
        git(d, "checkout", "-q", "--orphan", base, check=False)
        (d / "README.md").write_text(PUBLIC_README)
        wf = d / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "secret-scan.yml").write_text(SCAN_WORKFLOW)
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "Registry scaffold: README and secret-scan workflow")
    else:
        base = git(d, "rev-parse", "--abbrev-ref", "origin/HEAD", check=False).removeprefix("origin/") or "main"
    branch = f"seed/{time.strftime('%Y%m%d-%H%M%S')}"
    git(d, "checkout", "-q", "-b", branch)
    dest = d / "sops"
    added = []
    for src in sources:
        for item in sorted(src.rglob("*")):
            if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            target = dest / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.name == "sop.json":
                data = json.loads(item.read_text())
                for k in ("origin", "classified", "visibility", "stats"):
                    data.pop(k, None)
                data["scope"] = "public"
                target.write_text(json.dumps(data, indent=2) + "\n")
                added.append(data.get("id") or ".".join(item.parent.relative_to(src).parts))
            else:
                shutil.copy(item, target)
    build_index(dest)
    findings = scrub_tree(d, org)
    if findings:
        raise RegistryError("seed content failed the scan; nothing pushed:\n  " + "\n  ".join(findings))
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", f"Seed {len(added)} general-purpose SOPs from the Rameness starter package")
    out = {"path": str(d), "base": base, "branch": branch, "sops": added, "pushed": False}
    if push:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        r1 = subprocess.run(["git", "push", "-q", "origin", base], cwd=d, capture_output=True, text=True, env=env) \
            if empty else None
        r2 = subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=d, capture_output=True, text=True, env=env)
        if (r1 is None or r1.returncode == 0) and r2.returncode == 0:
            out["pushed"] = True
        else:
            out["push_error"] = ((r1.stderr if r1 and r1.returncode else "") + r2.stderr).strip()[-300:]
    m = GH_URL.search(url)
    if m:
        out["pr"] = f"https://github.com/{m.group(1)}/{m.group(2)}/compare/{base}...{branch}?expand=1"
    return out


def init_staging(dest: Path) -> Path:
    """Scaffold a staging registry repo (README, secret-scan workflow, empty index)."""
    (dest / "sops").mkdir(parents=True, exist_ok=True)
    (dest / "README.md").write_text(STAGING_README)
    wf = dest / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "secret-scan.yml").write_text(SCAN_WORKFLOW)
    build_index(dest / "sops")
    if not (dest / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    install_hook(dest)
    return dest
