"""Encrypted submission relay: how people *outside* Prog-Ramen get an SOP into the private intake.

A private repo can't accept pushes from the public, and giving outsiders access would show them
every other submission. Instead:

1. ``rameness sop submit <id>`` runs the same checks as ``propose`` (scrubber, secret scanners,
   JEV classification, sign-off when ambiguous), packs a sanitized copy, and **seals** it with
   Prog-Ramen's public key (RSA-OAEP-SHA256 wrapping an AES-256-GCM key).
2. It opens an issue on the public registry whose body is only the ciphertext. Anyone can see
   *that* someone submitted something; no one can read *what*.
3. The ``intake-relay`` workflow (in the public repo, holding the private key as a secret) unseals
   it, safely unpacks it (no code is executed), re-scans it, and opens a PR in the **private**
   intake repo, credited to the submitter. It then wipes the issue body and closes and locks the issue.

Submitted code is never run by the relay. Its tests run later, inside the private review.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

MAGIC = "RAMENESS-SOP-SUBMISSION v1"
MAX_SEALED = 60_000               # GitHub issue bodies are capped at 65,536 characters
MAX_FILES, MAX_BYTES = 40, 256_000


class RelayError(Exception):
    pass


# ---- crypto

def keygen(dest: Path) -> tuple[Path, Path]:
    """Create the intake keypair. The public key is committed to the public repo; the private key
    becomes the INTAKE_PRIVATE_KEY secret and should then be deleted from disk."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    dest.mkdir(parents=True, exist_ok=True)
    pub = dest / "intake_public_key.pem"
    priv = dest / "intake_private_key.pem"
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    priv.chmod(0o600)
    return pub, priv


def seal(public_pem: bytes, payload: bytes) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pub = serialization.load_pem_public_key(public_pem)
    k = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(k).encrypt(nonce, payload, MAGIC.encode())
    ek = pub.encrypt(k, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    blob = base64.b64encode(json.dumps({"ek": base64.b64encode(ek).decode(), "n": base64.b64encode(nonce).decode(),
                                        "ct": base64.b64encode(ct).decode()}).encode()).decode()
    text = f"{MAGIC}\n{blob}\n"
    if len(text) > MAX_SEALED:
        raise RelayError(f"submission too large ({len(text)} chars sealed; limit {MAX_SEALED})")
    return text


def unseal(private_pem: bytes, text: str) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    lines = text.strip().splitlines()
    if not lines or lines[0].strip() != MAGIC:
        raise RelayError("not a rameness submission")
    d = json.loads(base64.b64decode("".join(l.strip() for l in lines[1:])))
    key = serialization.load_pem_private_key(private_pem, password=None)
    k = key.decrypt(base64.b64decode(d["ek"]),
                    padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return AESGCM(k).decrypt(base64.b64decode(d["n"]), base64.b64decode(d["ct"]), MAGIC.encode())


# ---- packing

def pack(sop_dir: Path, meta: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        m = json.dumps(meta).encode()
        info = tarfile.TarInfo("meta.json")
        info.size = len(m)
        tar.addfile(info, io.BytesIO(m))
        for f in sorted(sop_dir.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                tar.add(f, arcname=f"sop/{f.relative_to(sop_dir)}", recursive=False)
    return buf.getvalue()


def unpack(data: bytes, dest: Path) -> dict:
    """Extract defensively: regular files only, no absolute paths or '..', bounded size."""
    meta, n, total = None, 0, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            name = m.name
            if not m.isfile() or name.startswith("/") or ".." in Path(name).parts:
                raise RelayError(f"rejected archive member: {name}")
            n += 1
            total += m.size
            if n > MAX_FILES or total > MAX_BYTES:
                raise RelayError("archive too large")
            content = tar.extractfile(m).read()
            if name == "meta.json":
                meta = json.loads(content)
                continue
            if not name.startswith("sop/"):
                raise RelayError(f"unexpected path {name}")
            out = dest / name[4:]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
    if meta is None or not (dest / "sop.json").exists():
        raise RelayError("submission is missing meta.json or sop.json")
    return meta


# ---- receive (runs in the public repo's workflow)

def receive(body: str, submitter: str, issue_ref: str, private_pem: bytes, intake_url: str, work: Path,
            org=None, open_pr=None) -> dict:
    """Unseal, check and forward one submission to the private intake repo. Never executes it."""
    from .publish import build_index, scrub_tree
    from .registry import git, install_hook
    submitter = re.sub(r"[^\w.-]", "-", submitter)[:39] or "anonymous"
    tmp = Path(tempfile.mkdtemp(prefix="rameness-relay-"))
    try:
        meta = unpack(unseal(private_pem, body), tmp / "sop")
        sop_id = re.sub(r"[^a-z0-9_.]", "_", str(meta.get("id", "")).lower()).strip("._")
        if not sop_id or "." not in sop_id:
            raise RelayError("submission has no valid SOP id")
        findings = scrub_tree(tmp / "sop", org)
        if findings:
            raise RelayError("submission failed the secret/private-data scan")   # details never posted publicly
        repo = work / "intake"
        if repo.exists():
            shutil.rmtree(repo)
        p = subprocess.run(["git", "clone", "-q", intake_url, str(repo)], capture_output=True, text=True)
        if p.returncode:
            raise RelayError(f"cannot clone intake: {p.stderr.strip()[:200]}")
        install_hook(repo)
        base = "main" if "origin/main" in git(repo, "branch", "-r", check=False) else None
        if base:
            git(repo, "checkout", "-q", "-B", base, f"origin/{base}")
        branch = f"sop/{submitter}/{sop_id}-{time.strftime('%Y%m%d%H%M%S')}"
        git(repo, "checkout", "-q", "-b", branch)
        target = repo / "sops" / Path(*sop_id.split("."))
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(tmp / "sop", target)
        build_index(repo / "sops")
        git(repo, "add", "-A")
        subprocess.run(["git", "-c", f"user.name={submitter}", "-c", f"user.email={submitter}@users.noreply.github.com",
                        "commit", "-q", "-m", f"Propose SOP {sop_id} (via encrypted relay, {issue_ref})"],
                       cwd=repo, check=True, capture_output=True)
        git(repo, "push", "-q", "-u", "origin", branch)
        cls = meta.get("classification", {})
        body_md = (f"## Proposed SOP `{sop_id}`\n\n{meta.get('description', '')}\n\n"
                   f"**Submitted by:** @{submitter} through the encrypted relay ({issue_ref})\n\n"
                   f"### JEV classification (on the submitter's machine)\n- Verdict: **{cls.get('visibility', '?')}**"
                   f"{' - submitter signed off' if meta.get('signed_off') else ''}\n- Reason: {cls.get('reason', '?')}\n"
                   f"- Organization-specific details found: {', '.join(cls.get('specific') or []) or 'none'}\n"
                   f"- Details JEV was unsure about: {', '.join(cls.get('uncertain') or []) or 'none'}\n\n"
                   "The relay re-scanned the files and did not execute anything. Run the SOP's tests in review.")
        pr = open_pr(branch, base or "main", f"SOP: {sop_id} (from @{submitter})", body_md) if open_pr else None
        return {"id": sop_id, "branch": branch, "pr": pr}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


RELAY_WORKFLOW = """name: intake-relay
# Forwards encrypted SOP submissions from public issues to the private intake repo.
on:
  issues: {types: [opened]}
permissions: {issues: write, contents: read}
concurrency: {group: intake-relay, cancel-in-progress: false}
jobs:
  relay:
    if: startsWith(github.event.issue.title, 'SOP submission') && startsWith(github.event.issue.body, 'RAMENESS-SOP-SUBMISSION v1')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install "git+https://github.com/{owner}/Rameness.git"
      - name: Unseal, scan and forward to the private intake
        env:
          # fine-grained token: contents + pull requests (write) on the intake repo, issues (write) here
          GH_TOKEN: "${{ secrets.INTAKE_RELAY_TOKEN }}"
          RAMENESS_INTAKE_PRIVATE_KEY: "${{ secrets.INTAKE_PRIVATE_KEY }}"
        run: |
          git config --global url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf "https://github.com/"
          rameness sop intake-receive "${{ github.event.issue.number }}" --repo "${{ github.repository }}" \\
            --intake "https://github.com/{owner}/{intake}.git"
"""


def init_relay(public_repo_dir: Path, owner: str = "Prog-Ramen", intake: str = "RamenSOPs-intake") -> dict:
    """Add the relay to a checkout of the public repo: workflow + public key. Returns key paths."""
    keys = public_repo_dir / ".github"
    pub, priv = keygen(Path(tempfile.mkdtemp(prefix="rameness-intake-key-")))
    (keys / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy(pub, keys / "intake_public_key.pem")
    (keys / "workflows" / "intake-relay.yml").write_text(
        RELAY_WORKFLOW.replace("{owner}", owner).replace("{intake}", intake))
    return {"public_key": str(keys / "intake_public_key.pem"), "private_key": str(priv)}
