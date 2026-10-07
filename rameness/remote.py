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
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path

from .jev import Jev, Option
from .sops import SOP

PULL_Q = "Pull this SOP from the remote registry for this task?"
PULL_CUES = "matches the task directly needed missing locally useful reusable procedure fits"
SKIP_CUES = "tangential unrelated not needed already covered different purpose side effects"


def pull_options(sop, activation: float) -> list[Option]:
    """The pull option carries the candidate's own description, and its activation score sets the prior."""
    return [Option("pull", f"{PULL_CUES} {sop.description} {' '.join(sop.keywords)}", 0.5 + activation,
                   f"Pull it: this procedure ({sop.description}) is directly needed for the task and missing locally."),
            Option("skip", SKIP_CUES, max(0.1, 1.5 - activation),
                   "Skip it: the procedure is tangential, not needed, or already covered locally.")]


class RemoteRegistry:
    def __init__(self, index_url: str, home: Path, name: str | None = None, ttl: float = 6 * 3600,
                 fanout: int = 16):
        self.url = index_url
        self.base = index_url.rsplit("/", 1)[0]
        self.name = name or (index_url.split("/")[4] if "githubusercontent.com" in index_url else "remote")
        self.cache_dir = home / "registry" / self.name
        self.install_root = home / "public" / self.name / "sops"
        self.ttl = ttl
        self.entries: dict[str, dict] = {}          # sop id -> listing entry (metadata + file hashes)
        self.nodes: dict[str, dict] = {}            # category id -> listing entry
        self.fetched: list[str] = []                # category listings downloaded (for tests / shadow)
        self.revalidated: list[str] = []            # stale listings confirmed unchanged (HTTP 304)
        self.stragglers: list[str] = []             # listings that missed a level's deadline
        self.fanout = fanout                        # parallel requests per tree level
        self.levels = 0                             # tree levels expanded (round trips) in the last search
        self._mem: dict[str, list] = {}
        self._lock = threading.Lock()

    # ---- lazy listings

    def listing(self, node_id: str, timeout: float = 10.0) -> list[dict]:
        """One category's listing: memory, then the disk cache while fresh, else the network. A stale cached copy
        is revalidated with its ETag / Last-Modified, so an unchanged listing costs a 304, not a download."""
        if node_id in self._mem:
            return self._mem[node_id]
        cache = self.cache_dir / "nodes" / f"{node_id or '_root'}.json"
        try:
            cached = json.loads(cache.read_text()) if cache.exists() else None
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached and time.time() - cached.get("_fetched", 0) < self.ttl:
            ents = cached["entries"]
        else:
            rel = "index.json" if not node_id else "/".join(node_id.split(".")) + "/_index.json"
            req = urllib.request.Request(f"{self.base}/{rel}")
            if cached and cached.get("etag"):
                req.add_header("If-None-Match", cached["etag"])
            if cached and cached.get("modified"):
                req.add_header("If-Modified-Since", cached["modified"])
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                    etag, modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
                ents = data.get("entries", [])
                self._store(cache, ents, etag, modified)
                self.fetched.append(node_id or "<root>")
            except urllib.error.HTTPError as e:
                if e.code == 304 and cached:                   # unchanged: keep it, fresh again
                    ents = cached["entries"]
                    self._store(cache, ents, cached.get("etag"), cached.get("modified"))
                    self.revalidated.append(node_id or "<root>")
                else:
                    ents = cached["entries"] if cached else []
            except Exception:
                ents = cached["entries"] if cached else []     # offline / not published: use what we have
        with self._lock:
            for e in ents:
                (self.entries if e.get("type") == "sop" else self.nodes)[e["id"]] = e
            self._mem[node_id] = ents
        return ents

    def _store(self, cache: Path, ents: list, etag: str | None, modified: str | None) -> None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"_fetched": time.time(), "entries": ents, "etag": etag, "modified": modified}))
        tmp.replace(cache)

    def listings(self, node_ids: list[str], deadline: float = 8.0) -> dict[str, list[dict]]:
        """Fetch several listings at once (one tree level, as Dremel's serving tree fans out to its leaves).
        A listing still missing at the deadline is a straggler: its stale cached copy is used if there is one,
        otherwise it is left out of this search and recorded in ``stragglers``."""
        todo = [n for n in node_ids if n not in self._mem]
        if todo:
            pool = ThreadPoolExecutor(max_workers=min(self.fanout, len(todo)))
            futures = {pool.submit(self.listing, n, deadline): n for n in todo}
            try:
                for f in as_completed(futures, timeout=deadline):
                    f.result()
            except TimeoutError:
                for f, n in futures.items():
                    if not f.done():
                        self.stragglers.append(n or "<root>")
                        stale = self._stale(n)
                        if stale is not None:
                            with self._lock:
                                self._mem.setdefault(n, stale)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        return {n: self._mem.get(n, []) for n in node_ids}

    def _stale(self, node_id: str) -> list[dict] | None:
        cache = self.cache_dir / "nodes" / f"{node_id or '_root'}.json"
        try:
            return json.loads(cache.read_text())["entries"]
        except (OSError, json.JSONDecodeError, KeyError):
            return None

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
        """Top-down activation over the remote tree, one level at a time: every category JEV chose to explore
        at this level is fetched in parallel, and their children are scored in parallel, one JEV decision per
        category exactly as a one-by-one walk would make them (so the choices don't depend on the backend).
        Round trips grow with the tree's depth, not with the number of categories opened."""
        from .sops import Node, Requirement, unresolved
        level: list[str] = [""]
        selected: list[tuple[SOP, float]] = []
        full = f"{task}\n{context}"
        self.levels = 0
        while level:
            self.levels += 1
            got = self.listings(level)
            parents = {n: [e for e in got[n] if not (e.get("type") == "sop" and e["id"] in installed)] for n in level}
            parents = {n: ents for n, ents in parents.items() if ents}
            if not parents:
                break

            def decide(n: str):
                return jev.activate(f"Which capabilities under '{n or 'root'}' will this task need?", task,
                                    [Option(e["id"], self._text(e), desc=e.get("description") or e["id"])
                                     for e in parents[n]])

            with ThreadPoolExecutor(max_workers=max(1, min(self.fanout, len(parents)))) as pool:
                decisions = dict(zip(parents, pool.map(decide, list(parents))))
            nxt: list[tuple[str, float]] = []
            for n, d in decisions.items():            # the beam applies within each parent, as before
                ranked = sorted(parents[n], key=lambda e: -d.probs[e["id"]])
                for rank, e in enumerate(ranked):
                    p = d.probs[e["id"]]
                    if p < explore_th or rank >= beam:
                        continue
                    if e.get("type") == "sop":
                        if p >= activate_th:
                            selected.append((self._stub(e), p))
                        continue
                    node = Node(e["id"], e.get("description", ""), e.get("keywords", []),
                                [Requirement(r["name"], r.get("hints", []), r.get("question", ""))
                                 for r in e.get("requires", [])])
                    if not unresolved(node, full, defaults):
                        nxt.append((e["id"], p))
            level = [nid for nid, _ in sorted(nxt, key=lambda x: -x[1])]
        return sorted(selected, key=lambda x: -x[1])[:max_sops]

    def candidates(self, jev: Jev, task: str, context: str, defaults: dict, installed: set[str],
                   activate_th: float, explore_th: float, beam: int) -> list[tuple[SOP, float]]:
        return self.traverse(jev, task, context, defaults, installed, activate_th, explore_th, beam)

    def entry(self, sop_id: str) -> dict | None:
        """Locate one SOP by walking only its ancestors' listings."""
        if sop_id not in self.entries:
            parts = sop_id.split(".")
            self.listings([".".join(parts[:i]) for i in range(len(parts))])   # all ancestors at once
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
        def download(f: dict) -> tuple[dict, bytes]:
            with urllib.request.urlopen(f"{self.base}/{f['path']}", timeout=20) as r:
                return f, r.read()

        try:
            files = e.get("files", [])
            with ThreadPoolExecutor(max_workers=max(1, min(self.fanout, len(files)))) as pool:
                for f, data in pool.map(download, files):      # all of the SOP's files at once
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
