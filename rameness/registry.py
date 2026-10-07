"""SOP registry: propose SOPs as pull requests to the public registry.

There is no private review step: a PR to a public repo is public the moment its branch is
pushed, so everything pushed must already be fit to publish::

    private library ──propose──► public repo PR (via a fork if you can't push) ──CI review──► merge
       (JEV: personal/general, JEV: private info in scrubber findings, secret scan)

The PR carries only the SOP (and any new category ``_node.json``); the registry rebuilds its
index on merge (see ``rameness.review`` for what CI checks).

Guards:

* only SOPs JEV classified ``shareable`` can be proposed (personal needs an explicit override)
* hard findings always block: secrets (scrubber + gitleaks/trufflehog when installed) and the
  organization's ``private_terms``
* soft findings (emails, private hosts/IPs, home paths) are judged by JEV: blocked only if it
  decides they expose private info
* every clone rameness manages gets a pre-push hook that re-scans the tree for hard findings

Config (``.rameness/config.json`` or ``~/.rameness/config.json``)::

    "registry": {"public": "git@github.com:Org/sops.git",
                 "fork":   null}          # push proposals here; default: origin, else a gh fork
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .org import Org
from .publish import classify, hard_findings, private_info, scrub, scrub_tree
from .sops import SOP, Library

GH_URL = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def fingerprint(sop: SOP) -> str:
    """Hash the public payload, excluding private bookkeeping and generated files."""
    digest = hashlib.sha256()
    for path in sorted(sop.path.rglob("*")):
        if not path.is_file() or any(p == "__pycache__" or p.endswith(".pyc") or p.startswith(".env")
                                    for p in path.relative_to(sop.path).parts):
            continue
        data = path.read_bytes()
        if path.name == "sop.json":
            meta = json.loads(data)
            for key in ("origin", "classified", "visibility", "stats"):
                meta.pop(key, None)
            meta["scope"] = "public"
            data = json.dumps(meta, sort_keys=True).encode()
        digest.update(json.dumps([path.relative_to(sop.path).as_posix(), data.hex()]).encode())
    return digest.hexdigest()


@contextmanager
def proposal_lock(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    with (home / "proposals.lock").open("a") as lock:
        if os.name == "posix":
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(lock, fcntl.LOCK_UN)


class RegistryError(Exception):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-c", "user.name=rameness", "-c", "user.email=rameness@localhost", *args],
                       cwd=cwd, capture_output=True, text=True, timeout=120)
    if check and p.returncode:
        raise RegistryError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def install_hook(repo: Path) -> None:
    """pre-push: re-scan the whole tree; any hard finding (secret, private term) blocks the push."""
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
        self.public = rc.get("public")
        self.fork = rc.get("fork")
        self.home = home / "registry"
        self.org, self.jev = org, jev
        self.ledger = self.home / "proposals.json"

    # ---- helpers

    def _clone(self, url: str, name: str) -> Path:
        d = self.home / name
        if not (d / ".git").exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            p = subprocess.run(["git", "clone", "-q", url, str(d)], capture_output=True, text=True, timeout=120)
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
        with tempfile.TemporaryDirectory(prefix="rameness-pr-") as temp:
            body_file = Path(temp) / "body.md"
            body_file.write_text(body)
            p = subprocess.run(["gh", "pr", "create", "--repo", f"{m.group(1)}/{m.group(2)}", "--head", branch,
                                "--base", base, "--title", title, "--body-file", str(body_file)],
                               capture_output=True, text=True, timeout=120)
        if p.returncode:
            # A previous create may have succeeded before the client lost its response.
            existing = subprocess.run(["gh", "pr", "list", "--repo", f"{m.group(1)}/{m.group(2)}",
                                       "--head", branch, "--state", "all", "--json", "url"],
                                      capture_output=True, text=True, timeout=120)
            if existing.returncode == 0:
                found = json.loads(existing.stdout)
                if found:
                    return found[0]["url"]
            raise RegistryError(f"PR creation failed: {(p.stderr or p.stdout).strip()}")
        if not p.stdout.strip():
            raise RegistryError("PR creation returned no URL")
        return p.stdout.strip()

    def _compare_url(self, url: str, branch: str, base: str) -> str | None:
        m = GH_URL.search(url)
        return f"https://github.com/{m.group(1)}/{m.group(2)}/compare/{base}...{branch}?expand=1" if m else None

    def proposals(self) -> list[dict]:
        return json.loads(self.ledger.read_text()) if self.ledger.exists() else []

    def _record(self, entry: dict) -> None:
        allp = [p for p in self.proposals() if not entry.get("key") or p.get("key") != entry["key"]] + [entry]
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        temp = self.ledger.with_suffix(".tmp")
        temp.write_text(json.dumps(allp, indent=1))
        temp.replace(self.ledger)

    # ---- propose (private library -> public PR)

    def _check(self, sop: SOP, override_personal: bool, reclassify: bool) -> dict:
        if sop.scope != "private":
            raise RegistryError(f"{sop.id} is already public")
        if sop.status != "validated":
            raise RegistryError(f"{sop.id} is {sop.status}: only SOPs whose tests pass can be proposed")
        findings = scrub(sop, self.org)
        hard = hard_findings(findings)
        if hard:
            raise RegistryError("secrets or private terms found - never proposable:\n  " + "\n  ".join(hard))
        leak = private_info(self.jev, sop, findings)
        if leak["leak"]:
            raise RegistryError(f"JEV judged these findings private (p={leak['p']:.2f}) - remove them first:\n  "
                                + "\n  ".join(leak["findings"]))
        cls = classify(self.jev, sop, self.org) if reclassify else {"visibility": sop.visibility, "reason": "stored"}
        if cls["visibility"] != "shareable" and not override_personal:
            raise RegistryError(f"JEV classified {sop.id} as personal ({cls['reason']}); it stays private. "
                                "If you are sure it is general, pass --override-personal (scans still apply).")
        cls["benign"] = leak["findings"]
        return cls

    def _push(self, repo: Path, url: str, branch: str) -> tuple[str, str]:
        """Push ``branch``; returns (PR head, pushed-to url). Falls back to a fork of a public target."""
        if not self.fork:
            p = subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=repo, capture_output=True,
                               text=True, timeout=120)
            if p.returncode == 0:
                return branch, url
            if not (GH_URL.search(url) and shutil.which("gh")):
                raise RegistryError(f"push to {url} failed: {(p.stderr or p.stdout).strip()}")
        fork = self.fork or self._gh_fork(url)
        git(repo, "remote", "remove", "fork", check=False)
        git(repo, "remote", "add", "fork", fork)
        git(repo, "push", "-q", "-u", "fork", branch)
        m = GH_URL.search(fork)
        return (f"{m.group(1)}:{branch}" if m else branch), fork

    def _gh_fork(self, url: str) -> str:
        owner, repo = GH_URL.search(url).groups()
        p = subprocess.run(["gh", "repo", "fork", f"{owner}/{repo}", "--clone=false"], capture_output=True,
                           text=True, timeout=120)
        me = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True,
                            timeout=120).stdout.strip()
        if not me:
            raise RegistryError(f"no push access to {url} and could not fork it: {p.stderr.strip()}")
        return f"https://github.com/{me}/{repo}.git"

    def propose(self, sop: SOP, override_personal: bool = False, reclassify: bool = True) -> dict:
        with proposal_lock(self.home):
            return self._propose(sop, override_personal, reclassify)

    def _propose(self, sop: SOP, override_personal: bool, reclassify: bool) -> dict:
        if not self.public:
            raise RegistryError("no registry configured (registry.public)")
        cls = self._check(sop, override_personal, reclassify)
        key = f"{self.public}:{sop.id}:{fingerprint(sop)}"
        previous = next((p for p in self.proposals() if p.get("key") == key), None)
        if previous and previous.get("status") in ("proposed", "pushed_local"):
            return previous
        if GH_URL.search(self.public) and not shutil.which("gh"):
            raise RegistryError("GitHub PR creation requires gh installed and authenticated")
        if previous and previous.get("status") == "pushed":
            pr = self._gh_pr(self.public, previous["head"], previous["base"], previous["title"], previous["body"])
            entry = {**previous, "pr": pr, "status": "proposed"}
            self._record(entry)
            return entry
        repo = self._clone(self.public, "public")
        branch = f"sop/{sop.id}-{time.time_ns()}"
        git(repo, "checkout", "-q", "-b", branch)
        sanitized_copy(sop, repo / "sops" / Path(*sop.id.split(".")))
        parts = sop.id.split(".")                # new categories travel with their SOP; the index is rebuilt on merge
        for i in range(1, len(parts)):
            src, dst = sop.path.parents[len(parts) - 1 - i] / "_node.json", repo / "sops" / Path(*parts[:i]) / "_node.json"
            if src.exists() and not dst.exists():
                shutil.copy(src, dst)
        leftover = hard_findings(scrub_tree(repo, self.org))
        if leftover:
            raise RegistryError("scan of the registry tree failed:\n  " + "\n  ".join(leftover))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"Propose SOP {sop.id}\n\n{sop.description}")
        head, pushed = self._push(repo, self.public, branch)
        body = (f"Proposed SOP `{sop.id}`: {sop.description}\n\nClassification: {cls['visibility']} ({cls['reason']}).\n"
                "Scans: rameness scrubber" + (" + gitleaks" if shutil.which("gitleaks") else "") +
                (" + trufflehog" if shutil.which("trufflehog") else "") + ": no secrets or private terms"
                + (f"; JEV judged {len(cls['benign'])} other finding(s) benign" if cls["benign"] else "") + ".")
        entry = {"id": sop.id, "key": key, "registry": self.public, "fingerprint": fingerprint(sop),
                 "branch": branch, "head": head, "base": self._base, "title": f"SOP: {sop.id}", "body": body,
                 "status": "pushed", "t": time.time(), "pushed": pushed, "pr": None,
                 "classification": cls["visibility"],
                 "overridden": cls["visibility"] != "shareable", "benign": cls["benign"]}
        # Persist the pushed branch before calling GitHub, so retries reuse it.
        self._record(entry)
        pr = self._gh_pr(self.public, head, self._base, entry["title"], body)
        entry.update(pr=pr or self._compare_url(self.public, head, self._base),
                     status="proposed" if pr else "pushed_local")
        self._record(entry)
        return entry

    def auto_propose(self, lib: Library, executor) -> list[dict]:
        """Drain eligible private SOPs, including those left by a previous failed attempt."""
        results = []
        for sop in list(lib.sops.values()):
            if sop.scope != "private" or sop.status != "validated" or sop.visibility != "shareable":
                continue
            try:
                key = f"{self.public}:{sop.id}:{fingerprint(sop)}"
                if any(p.get("key") == key and p.get("status") in ("proposed", "pushed_local")
                       for p in self.proposals()):
                    continue
                if not sop.tests:
                    raise RegistryError("no tests provided")
                if not any(isinstance(t.get("expect"), dict) and t["expect"] for t in sop.tests):
                    raise RegistryError("tests must assert expected output values before automatic publication; "
                                        "output keys or error cases alone are insufficient")
                failures = executor.test(sop.id)
                if failures:
                    raise RegistryError(f"tests failed: {failures}")
                proposal = self.propose(sop)
                results.append({"id": sop.id, "status": proposal["status"], "pr": proposal["pr"]})
            except Exception as e:
                results.append({"id": sop.id, "status": "failed", "error": str(e)})
        return results
