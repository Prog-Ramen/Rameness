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
from .deps import describe
from .publish import classify, destination, hard_findings, private_info, scrub, scrub_tree
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


def install_hook(repo: Path, subdir: str = "") -> None:
    """pre-push: re-scan the tree (or only ``subdir``, where proposals go: the Rameness repo's own tests
    carry fake secrets on purpose); any hard finding (secret, private term) blocks the push."""
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    where = "$(git rev-parse --show-toplevel)" + (f"/{subdir}" if subdir else "")
    hook.write_text(f"#!/bin/sh\n# installed by rameness: blocks pushes containing secrets or private data\n"
                    # -I: never import rameness from the clone itself (the Rameness repo has a rameness/ folder)
                    f"exec \"{sys.executable}\" -I -m rameness sop scrub-tree \"{where}\"\n")
    hook.chmod(0o755)


def sanitized_copy(sop: SOP, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sop.path, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*"))
    d = json.loads((dest / "sop.json").read_text())
    for k in ("origin", "classified", "visibility", "stats", "destination"):
        d.pop(k, None)
    d["scope"] = "public"
    (dest / "sop.json").write_text(json.dumps(d, indent=2) + "\n")


class Registry:
    def __init__(self, cfg: dict, home: Path, org: Org, jev):
        rc = cfg.get("registry") or {}
        self.public = rc.get("public")
        self.builtin = rc.get("builtin")          # the Rameness repo: built-in SOPs ship with Rameness itself
        self.builtin_min_share = rc.get("builtin_min_share", 0.5)
        self.builtin_min_runs = rc.get("builtin_min_runs", 10)
        self.fork = rc.get("fork")
        self.home = home / "registry"
        self.org, self.jev = org, jev
        self.ledger = self.home / "proposals.json"

    # ---- helpers

    # Where each kind of proposal goes: (repo url, folder the SOPs live in, pre-push scan scope)
    def _target(self, dest: str) -> tuple[str, str, str]:
        if dest == "builtin":
            if not self.builtin:
                raise RegistryError("no Rameness repo configured for built-in SOPs (registry.builtin)")
            return self.builtin, "rameness/builtin_sops", "rameness/builtin_sops"
        if not self.public:
            raise RegistryError("no registry configured (registry.public)")
        return self.public, "sops", ""

    def destination(self, sop: SOP) -> dict:
        """builtin or registry for a shareable SOP, from how many runs used it (publish.destination).
        An extended copy of an existing SOP goes back to where the original lives, as an update."""
        overrides = (sop.origin or {}).get("overrides")
        if overrides in ("builtin", "registry"):
            return {"destination": overrides, "reason": f"updates the existing {overrides} SOP {sop.id}"}
        if not self.builtin:
            return {"destination": "registry", "reason": "no Rameness repo configured for built-in SOPs"}
        o = sop.origin or {}
        d = destination(self.jev, sop, int(o.get("runs_seen", 0)), int(o.get("runs_total", 0)),
                        self.builtin_min_share, self.builtin_min_runs)
        if sop.scope == "private" and (sop.path / "sop.json").exists():
            data = json.loads((sop.path / "sop.json").read_text())
            data["destination"] = d["destination"]
            (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
        return d

    def _clone(self, url: str, name: str, scan: str = "") -> Path:
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
        install_hook(d, scan)
        return d

    def _gh_pr(self, url: str, branch: str, base: str, title: str, body: str, draft: bool = False) -> str | None:
        m = GH_URL.search(url)
        if not (m and shutil.which("gh")):
            return None
        with tempfile.TemporaryDirectory(prefix="rameness-pr-") as temp:
            body_file = Path(temp) / "body.md"
            body_file.write_text(body)
            p = subprocess.run(["gh", "pr", "create", "--repo", f"{m.group(1)}/{m.group(2)}", "--head", branch,
                                "--base", base, "--title", title, "--body-file", str(body_file)]
                               + (["--draft"] if draft else []),
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

    def propose(self, sop: SOP, override_personal: bool = False, reclassify: bool = True,
                to: str | None = None) -> dict:
        """Open a PR for ``sop``: to the Rameness repo when it is built-in material, else to the registry.
        ``to`` (builtin | registry) overrides the decision."""
        with proposal_lock(self.home):
            return self._propose(sop, override_personal, reclassify, to)

    def _propose(self, sop: SOP, override_personal: bool, reclassify: bool, to: str | None = None) -> dict:
        if not (self.public or self.builtin):
            raise RegistryError("no registry configured (registry.public)")
        cls = self._check(sop, override_personal, reclassify)
        dest = {"destination": to, "reason": "chosen by the user"} if to else self.destination(sop)
        url, prefix, scan = self._target(dest["destination"])
        key = f"{url}:{sop.id}:{fingerprint(sop)}"
        previous = next((p for p in self.proposals() if p.get("key") == key), None)
        if previous and previous.get("status") in ("proposed", "pushed_local"):
            return previous
        if GH_URL.search(url) and not shutil.which("gh"):
            raise RegistryError("GitHub PR creation requires gh installed and authenticated")
        if previous and previous.get("status") == "pushed":
            pr = self._gh_pr(url, previous["head"], previous["base"], previous["title"], previous["body"],
                             draft=previous.get("destination") == "builtin")
            entry = {**previous, "pr": pr, "status": "proposed"}
            self._record(entry)
            return entry
        repo = self._clone(url, dest["destination"] if dest["destination"] == "builtin" else "public", scan)
        branch = f"sop/{sop.id}-{time.time_ns()}"
        git(repo, "checkout", "-q", "-b", branch)
        base_dir = repo / prefix
        sanitized_copy(sop, base_dir / Path(*sop.id.split(".")))
        parts = sop.id.split(".")                # new categories travel with their SOP; the index is rebuilt on merge
        for i in range(1, len(parts)):
            src, dst = sop.path.parents[len(parts) - 1 - i] / "_node.json", base_dir / Path(*parts[:i]) / "_node.json"
            if src.exists() and not dst.exists():
                shutil.copy(src, dst)
        leftover = hard_findings(scrub_tree(repo / scan if scan else repo, self.org))
        if leftover:
            raise RegistryError("scan of the registry tree failed:\n  " + "\n  ".join(leftover))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"Propose SOP {sop.id}\n\n{sop.description}")
        head, pushed = self._push(repo, url, branch)
        builtin = dest["destination"] == "builtin"
        kind = "built-in SOP (ships with Rameness)" if builtin else "SOP"
        needs = describe(sop.requirements)
        body = (f"Proposed {kind} `{sop.id}`: {sop.description}\n\n"
                f"Classification: {cls['visibility']} ({cls['reason']}).\n"
                f"Destination: {dest['destination']} ({dest['reason']}).\n"
                + (f"{needs}\n" if needs else "Needs nothing beyond Python and a POSIX shell.\n") +
                "Scans: rameness scrubber" + (" + gitleaks" if shutil.which("gitleaks") else "") +
                (" + trufflehog" if shutil.which("trufflehog") else "") + ": no secrets or private terms"
                + (f"; JEV judged {len(cls['benign'])} other finding(s) benign" if cls["benign"] else "") + "."
                + ("\n\n**Needs verification by a Rameness developer before merging.** Built-in SOPs ship to "
                   "every Rameness user; this was proposed automatically and opened as a draft." if builtin else ""))
        entry = {"id": sop.id, "key": key, "registry": url, "destination": dest["destination"],
                 "fingerprint": fingerprint(sop),
                 "branch": branch, "head": head, "base": self._base, "title": f"SOP: {sop.id}", "body": body,
                 "status": "pushed", "t": time.time(), "pushed": pushed, "pr": None,
                 "classification": cls["visibility"],
                 "overridden": cls["visibility"] != "shareable", "benign": cls["benign"]}
        # Persist the pushed branch before calling GitHub, so retries reuse it.
        self._record(entry)
        pr = self._gh_pr(url, head, self._base, entry["title"], body, draft=builtin)
        entry.update(pr=pr or self._compare_url(url, head, self._base),
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
                fp = fingerprint(sop)
                if any(p.get("key", "").endswith(f":{sop.id}:{fp}") and p.get("status") in ("proposed", "pushed_local")
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
