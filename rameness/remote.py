"""Pulling only the SOPs a task needs from a remote registry (e.g. RamenSOPs).

Nothing is mirrored. The registry index is sharded per category, and JEV walks it lazily with
the same activation criteria it uses on the local library:

1. Local first. The remote registry is consulted only when no local SOP clearly covers the
   task (best local activation below ``activate_threshold + coverage_margin``).
2. Fetch the root listing (top-level categories only). JEV scores those children in one call;
   rejected branches are never downloaded, and a category whose ``requires`` aren't satisfied is
   deferred without being opened. Only categories JEV explores have their ``_index.json`` fetched,
   and so on down the tree.
3. Each SOP leaf above the activation threshold becomes a candidate. JEV makes a pull decision,
   followed by the comfort gate. Permissions outside ``permissions.sop_allow`` need the user's yes;
   without a user present the SOP is not pulled.
4. Only the chosen SOP's files are downloaded, each checked against its SHA-256 from the listing,
   and its tests must pass before it is used; otherwise it is removed again.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.request
from pathlib import Path

from .jev import Jev, Option
from .sops import SOP

PULL_Q = "Pull this SOP from the remote registry for this task?"
PULL_CUES = "matches the task directly needed missing locally useful reusable procedure fits"
SKIP_CUES = "tangential unrelated not needed already covered different purpose side effects"


def pull_options(sop, activation: float) -> list[Option]:
    """The pull option carries the candidate's own description, and its activation score sets the prior."""
    return [Option("pull", f"{PULL_CUES} {sop.description} {' '.join(sop.keywords)}", 0.5 + activation),
            Option("skip", SKIP_CUES, max(0.1, 1.5 - activation))]


class RemoteRegistry:
    def __init__(self, index_url: str, home: Path, name: str | None = None, ttl: float = 6 * 3600):
        self.url = index_url
        self.base = index_url.rsplit("/", 1)[0]
        self.name = name or (index_url.split("/")[4] if "githubusercontent.com" in index_url else "remote")
        self.cache_dir = home / "registry" / self.name
        self.install_root = home / "public" / self.name / "sops"
        self.ttl = ttl
        self.entries: dict[str, dict] = {}          # sop id -> listing entry (metadata + file hashes)
        self.nodes: dict[str, dict] = {}            # category id -> listing entry
        self.fetched: list[str] = []                # category listings downloaded (for tests / shadow)
        self._mem: dict[str, list] = {}

    # ---- lazy listings

    def listing(self, node_id: str) -> list[dict]:
        if node_id in self._mem:
            return self._mem[node_id]
        cache = self.cache_dir / "nodes" / f"{node_id or '_root'}.json"
        cached = json.loads(cache.read_text()) if cache.exists() else None
        if cached and time.time() - cached.get("_fetched", 0) < self.ttl:
            ents = cached["entries"]
        else:
            rel = "index.json" if not node_id else "/".join(node_id.split(".")) + "/_index.json"
            try:
                with urllib.request.urlopen(f"{self.base}/{rel}", timeout=10) as r:
                    data = json.loads(r.read())
                ents = data.get("entries", [])
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"_fetched": time.time(), "entries": ents}))
                self.fetched.append(node_id or "<root>")
            except Exception:
                ents = cached["entries"] if cached else []     # offline / not published: use what we have
        for e in ents:
            (self.entries if e.get("type") == "sop" else self.nodes)[e["id"]] = e
        self._mem[node_id] = ents
        return ents

    @staticmethod
    def _text(e: dict) -> str:
        if e.get("type") == "sop":
            return f"{e['id'].replace('.', ' ')} {e.get('description', '')} {' '.join(e.get('keywords', []))}"
        return (f"{e['id'].split('.')[-1]} {e.get('description', '')} {' '.join(e.get('keywords', []))} "
                f"{' '.join(e.get('children', []))}")

    @staticmethod
    def _stub(e: dict) -> SOP:
        return SOP(id=e["id"], path=Path("remote:" + e.get("path", e["id"])), description=e.get("description", ""),
                   kind=e.get("kind", "script"), inputs=e.get("inputs") or {"type": "object", "properties": {}},
                   permissions=e.get("permissions", []), keywords=e.get("keywords", []),
                   status=e.get("status", "validated"), scope="remote", version=e.get("version", "1.0.0"))

    def traverse(self, jev: Jev, task: str, context: str, defaults: dict, installed: set[str],
                 activate_th: float, explore_th: float, beam: int, max_sops: int = 3) -> list[tuple[SOP, float]]:
        """Top-down activation over the remote tree, opening only the branches JEV explores."""
        from .sops import Node, Requirement, unresolved
        frontier: list[tuple[str, float]] = [("", 1.0)]
        selected: list[tuple[SOP, float]] = []
        full = f"{task}\n{context}"
        while frontier:
            frontier.sort(key=lambda x: -x[1])
            nid, _ = frontier.pop(0)
            ents = [e for e in self.listing(nid) if not (e.get("type") == "sop" and e["id"] in installed)]
            if not ents:
                continue
            d = jev.activate(f"Which capabilities under '{nid or 'root'}' will this task need?", task,
                             [Option(e["id"], self._text(e)) for e in ents], context)
            ranked = sorted(ents, key=lambda e: -d.probs[e["id"]])
            for rank, e in enumerate(ranked):
                p = d.probs[e["id"]]
                if p < explore_th or rank >= beam:
                    continue
                if e.get("type") == "sop":
                    if p >= activate_th:
                        selected.append((self._stub(e), p))
                    continue
                node = Node(e["id"], e.get("description", ""), e.get("keywords", []),
                            [Requirement(r["name"], r.get("hints", []), r.get("question", "")) for r in e.get("requires", [])])
                if not unresolved(node, full, defaults):
                    frontier.append((e["id"], p))
        return sorted(selected, key=lambda x: -x[1])[:max_sops]

    def candidates(self, jev: Jev, task: str, context: str, defaults: dict, installed: set[str],
                   activate_th: float, explore_th: float, beam: int) -> list[tuple[SOP, float]]:
        return self.traverse(jev, task, context, defaults, installed, activate_th, explore_th, beam)

    def entry(self, sop_id: str) -> dict | None:
        """Locate one SOP by walking only its ancestors' listings."""
        if sop_id not in self.entries:
            parts = sop_id.split(".")
            for i in range(len(parts)):
                self.listing(".".join(parts[:i]))
        return self.entries.get(sop_id)

    # ---- install only what was chosen

    def fetch(self, sop_id: str) -> Path:
        """Download one SOP, verifying every file against the listing's hash."""
        e = self.entry(sop_id)
        if not e:
            raise KeyError(f"{sop_id} is not in the remote registry")
        dest = self.install_root.joinpath(*e["path"].split("/"))
        tmp = dest.with_name(dest.name + ".partial")
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            for f in e.get("files", []):
                with urllib.request.urlopen(f"{self.base}/{f['path']}", timeout=20) as r:
                    data = r.read()
                if hashlib.sha256(data).hexdigest() != f["sha256"]:
                    raise ValueError(f"hash mismatch for {f['path']}: refusing to install {sop_id}")
                rel = Path(f["path"]).relative_to(e["path"])
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_bytes(data)
            if not (tmp / "sop.json").exists():
                raise ValueError(f"{sop_id}: no sop.json in the listing's file list")
            self._copy_nodes(e["path"])
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
        return dest

    def _copy_nodes(self, sop_path: str) -> None:
        """Bring the ancestors' category descriptions along so the local tree stays navigable."""
        parts = sop_path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            node = self.nodes.get(".".join(parts[:i]))
            if node:
                d = self.install_root.joinpath(*parts[:i])
                d.mkdir(parents=True, exist_ok=True)
                (d / "_node.json").write_text(json.dumps({k: node.get(k) for k in ("description", "keywords", "requires")}))

    def remove(self, sop_id: str) -> None:
        e = self.entry(sop_id)
        if e:
            shutil.rmtree(self.install_root.joinpath(*e["path"].split("/")), ignore_errors=True)
