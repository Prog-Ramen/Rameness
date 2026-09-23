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
import tempfile
import time
import urllib.error
import uuid
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
        self.relay_repo = rc.get("relay_repo") or "Prog-Ramen/RamenSOPs"
        self.relay_key = rc.get("relay_key")
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

    @staticmethod
    def _contributor() -> str:
        for cmd in (["gh", "api", "user", "-q", ".login"], ["git", "config", "user.name"]):
            if shutil.which(cmd[0]):
                p = subprocess.run(cmd, capture_output=True, text=True)
                if p.returncode == 0 and p.stdout.strip():
                    return re.sub(r"[^\w.-]", "-", p.stdout.strip())[:39]
        return "anonymous"

    def proposals(self) -> list[dict]:
        return json.loads(self.ledger.read_text()) if self.ledger.exists() else []

    def _record(self, entry: dict) -> None:
        allp = self.proposals() + [entry]
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(allp, indent=1))

    # ---- propose (private library -> private staging PR)

    def check(self, sop: SOP, override_personal: bool = False, reclassify: bool = True,
              sign_off=None) -> tuple[dict, bool]:
        """Everything that must hold before an SOP leaves the machine. Returns (classification, signed_off)."""
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
        signed = False
        if cls["visibility"] == "ambiguous":
            files = sorted(str(f.relative_to(sop.path)) for f in sop.path.rglob("*") if f.is_file()
                           and "__pycache__" not in f.parts)
            ok = sign_off(sop, cls, files) if callable(sign_off) else bool(sign_off)
            if not ok:
                raise RegistryError(f"{sop.id} is ambiguous ({cls['reason']}) and needs your sign-off: review the "
                                    "files and JEV's evidence, then confirm (or pass --sign-off).")
            signed = True
        elif cls["visibility"] != "shareable" and not override_personal:
            raise RegistryError(f"{sop.id} was classified as not shareable ({cls['reason']}); it stays private. "
                                "If you are sure it is general, pass --override-personal: it still goes only to "
                                "the private intake review.")
        return cls, signed

    def propose(self, sop: SOP, override_personal: bool = False, reclassify: bool = True,
                sign_off=None) -> dict:
        """For Prog-Ramen members (write access to the private intake repo). ``sign_off``: for
        ambiguous SOPs, a callable(sop, classification, files) -> bool that shows the contributor
        exactly what would be submitted and JEV's evidence (or True for a prior explicit sign-off)."""
        if not self.staging:
            raise RegistryError("no intake repo configured (registry.staging) - it must be a PRIVATE repo")
        cls, signed = self.check(sop, override_personal, reclassify, sign_off)
        ok, how = visibility(self.staging, self.assume_private)
        if not ok:
            raise RegistryError(f"refusing to push: staging repo is not verifiably private ({how})")
        repo = self._clone(self.staging, "staging")
        who = self._contributor()
        branch = f"sop/{who}/{sop.id}-{time.strftime('%Y%m%d%H%M%S')}"
        git(repo, "checkout", "-q", "-b", branch)
        sanitized_copy(sop, repo / "sops" / Path(*sop.id.split(".")))
        build_index(repo / "sops")
        leftover = scrub_tree(repo, self.org)
        if leftover:
            raise RegistryError("scan of the staging tree failed:\n  " + "\n  ".join(leftover))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"Propose SOP {sop.id}\n\n{sop.description}")
        git(repo, "push", "-q", "-u", "origin", branch)
        probs = ", ".join(f"{k} {v:.0%}" for k, v in sorted(cls.get("probs", {}).items(), key=lambda kv: -kv[1]))
        body = (f"## Proposed SOP `{sop.id}`\n\n{sop.description}\n\n"
                f"**Contributor:** {who}  \n**Permissions:** {', '.join(sop.permissions) or 'none'}  \n"
                f"**Tests:** {len(sop.tests)} (passing)\n\n"
                f"### JEV classification\n- Verdict: **{cls['visibility']}**"
                f"{' - contributor signed off after reviewing the files and evidence' if signed else ''}"
                f"{' (overridden by the contributor)' if override_personal and cls['visibility'] == 'private' else ''}\n"
                f"- Reason: {cls['reason']}\n- Probabilities: {probs or 'n/a'}\n"
                f"- Organization-specific details JEV found: {', '.join(cls.get('specific') or []) or 'none'}\n"
                f"- Details JEV was unsure about: {', '.join(cls.get('uncertain') or []) or 'none'}\n\n"
                "### Scans\nrameness scrubber" + (" + gitleaks" if shutil.which("gitleaks") else "") +
                (" + trufflehog" if shutil.which("trufflehog") else "") + ": clean.\n\n"
                "### Reviewer checklist\n- [ ] Nothing here identifies an organization, customer, person or internal system\n"
                "- [ ] Constants are generic defaults or parameters, not one company's business rules\n"
                "- [ ] Useful beyond the contributor's own use case\n\n"
                "This repository is private. Merging here publishes nothing by itself: the release workflow "
                "re-scans merged SOPs and opens a release PR on the public registry.")
        pr = self._gh_pr(self.staging, branch, self._base, f"SOP: {sop.id}", body)
        entry = {"id": sop.id, "branch": branch, "t": time.time(), "staging": self.staging, "where": how,
                 "pr": pr or self._compare_url(self.staging, branch, self._base), "classification": cls["visibility"],
                 "signed_off": signed, "overridden": cls["visibility"] == "private"}
        self._record(entry)
        return entry

    # ---- submit (anyone: encrypted relay through a public issue)

    def submit(self, sop: SOP, override_personal: bool = False, sign_off=None, create_issue=None) -> dict:
        """For contributors without access to the intake repo: seal the SOP with the intake's public
        key and open an issue on the public repo containing only ciphertext."""
        from . import relay
        import urllib.request
        cls, signed = self.check(sop, override_personal, True, sign_off)
        src = self.relay_key
        if not src:
            raise RegistryError("no relay key configured (registry.relay_key)")
        try:
            if re.match(r"(https?|file)://", src):
                with urllib.request.urlopen(src, timeout=15) as r:
                    pem = r.read()
            else:
                pem = Path(src).expanduser().read_bytes()
        except Exception as e:
            raise RegistryError(f"cannot load the intake public key from {src}: {e}")
        tmp = Path(tempfile.mkdtemp(prefix="rameness-submit-"))
        try:
            sanitized_copy(sop, tmp / "sop")
            meta = {"id": sop.id, "description": sop.description, "signed_off": signed,
                    "classification": {k: cls.get(k) for k in ("visibility", "reason", "specific", "uncertain")}}
            body = relay.seal(pem, relay.pack(tmp / "sop", meta))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        title = f"SOP submission {uuid.uuid4().hex[:8]}"            # reveals nothing about the SOP
        if create_issue:
            url = create_issue(self.relay_repo, title, body)
        elif shutil.which("gh"):
            p = subprocess.run(["gh", "issue", "create", "--repo", self.relay_repo, "--title", title, "--body-file", "-"],
                               input=body, capture_output=True, text=True)
            if p.returncode:
                raise RegistryError(f"could not open the submission issue: {p.stderr.strip()}")
            url = p.stdout.strip()
        else:
            out = self.home / f"{title.replace(' ', '-')}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(body)
            url = f"(no gh) open an issue on {self.relay_repo} titled '{title}' with the contents of {out}"
        entry = {"id": sop.id, "via": "relay", "issue": url, "t": time.time(), "signed_off": signed}
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


INTAKE_README = """# RamenSOPs intake (PRIVATE)

Review queue for SOPs proposed to the public [RamenSOPs](https://github.com/Prog-Ramen/RamenSOPs)
registry. **Keep this repository private**: only Prog-Ramen members with access can see the
proposals here.

1. A contributor runs `rameness sop propose <id>`. JEV must have classified the SOP as shareable
   (or the contributor overrides), and the scrubber and secret scanners must be clean. The PR body
   shows JEV's verdict, the organization-specific details it looked at, and its probabilities.
2. A reviewer (see CODEOWNERS) checks the PR against the checklist and merges or closes it.
3. On merge, `release-to-public` re-scans the merged SOPs and opens a release PR on RamenSOPs.

Everyone with read access here can see every proposal. Grant access only to trusted members,
not to outside contributors.
"""

CODEOWNERS = "* @{owner}/sop-reviewers\n"

PR_TEMPLATE = """### Reviewer checklist
- [ ] Nothing identifies an organization, customer, person or internal system
- [ ] Constants are generic defaults or parameters, not one company's business rules
- [ ] Useful beyond the contributor's own use case
- [ ] Tests pass and the SOP's permissions are the minimum it needs
"""

RELEASE_WORKFLOW = """name: release-to-public
on:
  push: {branches: [main]}
  workflow_dispatch:
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: gitleaks/gitleaks-action@v2
        env: {GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}"}
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install "git+https://github.com/{owner}/Rameness.git"
      - name: Re-scan merged SOPs and open a release PR on the public registry
        env:
          # fine-grained token: contents + pull requests (write) on the public registry, read on this repo
          GH_TOKEN: "${{ secrets.RAMENSOPS_RELEASE_TOKEN }}"
        run: |
          git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"
          mkdir -p ~/.rameness
          cat > ~/.rameness/config.json <<JSON
          {"registry": {"staging": "https://github.com/${{ github.repository }}.git",
                        "public": "https://github.com/{public}.git", "release": "pr"}}
          JSON
          rameness sop release
"""

STAGING_README = INTAKE_README

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


def init_staging(dest: Path, owner: str = "Prog-Ramen", public: str = "Prog-Ramen/RamenSOPs") -> Path:
    """Scaffold an org-owned private intake repo: README, CODEOWNERS, PR checklist,
    secret-scan CI and the release-on-merge workflow."""
    (dest / "sops").mkdir(parents=True, exist_ok=True)
    (dest / "README.md").write_text(INTAKE_README)
    gh = dest / ".github"
    (gh / "workflows").mkdir(parents=True, exist_ok=True)
    (gh / "CODEOWNERS").write_text(CODEOWNERS.replace("{owner}", owner))
    (gh / "pull_request_template.md").write_text(PR_TEMPLATE)
    (gh / "workflows" / "secret-scan.yml").write_text(SCAN_WORKFLOW)
    (gh / "workflows" / "release-to-public.yml").write_text(
        RELEASE_WORKFLOW.replace("{owner}", owner).replace("{public}", public))
    build_index(dest / "sops")
    if not (dest / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    install_hook(dest)
    return dest
