"""Keep the SOP tree efficient as it grows, like a Dremel serving tree: every category stays small, so finding
an SOP means a few small listings and a few JEV questions per level, never a scan of one huge category.

* place: a new SOP is filed by walking the tree level by level (JEV picks the subcategory it belongs in, or
  "here"); a proposed deeper path is kept, never flattened.
* rebalance: a category with more than ``limit`` SOPs directly in it splits. The model proposes groups of
  related SOPs (it identifies); JEV confirms each member (it decides); each group becomes a subcategory with
  its own description. A subcategory left with fewer than ``min_size`` SOPs folds back into its parent.
  Moved SOPs keep their old ids as aliases, keep their stats, and must still pass their tests.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import schemas
from .jev import Jev, Option
from .sops import SOP, Library, Node

LIMIT = 8          # most SOPs directly in one category before it splits (Dremel: bounded work per server)
MIN_SIZE = 3       # a subcategory with fewer folds back into its parent (hysteresis: no split/fold churn)

GROUP_SYSTEM = """You organize a category of standard procedures (SOPs) that has grown too large. Group its
procedures into a few subcategories of closely related procedures, so that someone looking for one of them can pick
the right subcategory from its name and description alone. Each subcategory: "name" (snake_case, one or two
words, e.g. "lint", "browser", "csv"), "description" (one line: what its procedures do), "keywords" (a few
search words) and "members" (the ids of its procedures). A procedure that fits an existing subcategory goes in
that one (use its name). A new group needs at least three members; a procedure that fits no group stays unlisted.
Reply {"groups": [...]}. Output JSON only."""


def node_at(lib: Library, category: str) -> Node | None:
    n = lib.root
    for part in [p for p in category.split(".") if p]:
        n = n.children.get(part)
        if n is None:
            return None
    return n


def place(jev: Jev, lib: Library, sop: SOP, min_p: float = 0.5) -> str:
    """The category an SOP belongs in, found by walking the tree from the top: at each level JEV picks the
    subcategory it fits, or "here" (none of them). The SOP's proposed path (its id minus the name) is offered
    where it names a category that does not exist yet. Returns the category id ("" never: at least one level)."""
    proposed = [p for p in sop.id.split(".")[:-1] if p]
    node, path = lib.root, []
    query = f"{sop.id}: {sop.description}"
    while True:
        cats = {cid: c for cid, c in node.children.items() if c.sop is None and cid != "learned"}
        want = proposed[len(path)] if len(proposed) > len(path) else None
        opts = [Option(cid, c.text, desc=c.desc) for cid, c in cats.items()]
        if want and want not in cats and want != "learned":
            opts.append(Option(want, f"{want} new category {sop.text}",
                               desc=f"A new category, '{want}', for procedures like this one"))
        if path:
            opts.append(Option("__here__", f"{node.id.split('.')[-1]} general other none of these",
                               desc=f"Directly in '{node.id}': none of its subcategories fits"))
        if not opts or (len(opts) == 1 and opts[0].id == "__here__"):
            break
        if not cats and not path:                     # an empty library: the proposal, or a catch-all
            return want or "learned"
        d = jev.choose(f"Which category of standard procedures does this procedure belong in"
                       f"{' (inside ' + node.id + ')' if path else ''}?", query, opts)
        best = max(d.probs, key=d.probs.get)
        if best == "__here__":
            break
        if best not in cats:                          # a new category: here is where it starts
            if d.probs[best] >= min_p or not cats:
                path.append(best)
            elif not path:
                path.append(max(cats, key=lambda c: d.probs.get(c, 0)))
            break
        if d.probs[best] < min_p and path:            # not clearly any subcategory: stay at this level
            break
        if d.probs[best] < min_p and want and want not in cats:
            path.append(want)                         # not clearly an existing one: the proposal stands
            break
        path.append(best)
        node = cats[best]
    return ".".join(path) or (proposed[0] if proposed else "learned")


# ---- aliases: a moved SOP keeps answering to its old id

def _aliases_file(root: Path) -> Path:
    return root / "_aliases.json"


def add_alias(lib: Library, root: Path, old: str, new: str) -> None:
    f = _aliases_file(root)
    data = json.loads(f.read_text()) if f.exists() else {}
    data = {k: (new if v == old else v) for k, v in data.items()}   # earlier aliases follow the move too
    data[old] = new
    data.pop(new, None)
    f.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    if old in lib.stats:                          # its track record moves with it
        lib.stats[new] = lib.stats.pop(old)
        if lib.stats_path:
            lib.stats_path.write_text(json.dumps(lib.stats, indent=1))


def move(lib: Library, root: Path, sop: SOP, new_id: str) -> SOP | None:
    """Move an SOP folder to ``new_id``'s path under ``root``, rewrite its id, keep its old id as an alias."""
    target = root.joinpath(*new_id.split("."))
    if target.exists() or new_id == sop.id:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(sop.path), str(target))
    data = json.loads((target / "sop.json").read_text())
    data["id"] = new_id
    (target / "sop.json").write_text(json.dumps(data, indent=2) + "\n")
    add_alias(lib, root, sop.id, new_id)
    _prune(sop.path.parent, root)
    lib.reload()
    return lib.get(new_id)


def _prune(d: Path, root: Path) -> None:
    """Remove category folders a move left empty (only metadata left)."""
    while d != root and d.is_relative_to(root) and d.exists():
        if any(c.is_dir() and not c.name.startswith((".", "_")) for c in d.iterdir()):
            return
        shutil.rmtree(d)
        d = d.parent


def _root_of(lib: Library, sop: SOP) -> Path | None:
    return next((r for r, scope in lib.roots if scope == "private" and sop.path.is_relative_to(r)), None)


def _node_meta(d: Path, description: str, keywords: list[str]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "_node.json").write_text(json.dumps({"description": description, "keywords": keywords}, indent=1) + "\n")


def rebalance(lib: Library, ex, category: str, llm, jev: Jev, limit: int = LIMIT, min_size: int = MIN_SIZE) -> dict:
    """Split ``category`` if more than ``limit`` of its own (private) SOPs sit directly in it; fold back a
    subcategory that has fewer than ``min_size``. Only folders on disk this library may change move (private
    roots; a registry checkout passed as a private folder). Returns what moved."""
    out = {"category": category, "split": [], "folded": [], "kept": []}
    node = node_at(lib, category)
    if node is None or node.sop is not None:
        return out
    leaves = [c.sop for c in node.children.values() if c.sop and _root_of(lib, c.sop)]
    if len(leaves) > limit and llm is not None:
        out["split"] = _split(lib, ex, node, leaves, llm, jev, min_size)
    node = node_at(lib, category)
    for sub in [c for c in (node.children.values() if node else []) if c.sop is None]:
        kids = [c.sop for c in sub.children.values() if c.sop]
        own = [s for s in kids if _root_of(lib, s)]
        if 0 < len(kids) < min_size and len(own) == len(kids) and not any(c.sop is None for c in sub.children.values()):
            here = sum(1 for c in node_at(lib, category).children.values() if c.sop)
            if here + len(own) <= limit:
                for s in own:
                    moved = move(lib, _root_of(lib, s), s, f"{category}.{s.id.split('.')[-1]}")
                    if moved:
                        out["folded"].append(f"{s.id} -> {moved.id}")
    return out


def _split(lib: Library, ex, node: Node, leaves: list[SOP], llm, jev: Jev, min_size: int = MIN_SIZE) -> list[str]:
    subs = {cid: c for cid, c in node.children.items() if c.sop is None}
    listing = "\n".join(f"- {s.id}: {s.description[:240]}" for s in leaves)
    existing = "\n".join(f"- {cid}: {c.description}" for cid, c in subs.items()) or "none"
    try:
        r = llm.complete_json(GROUP_SYSTEM, (
            f"Category '{node.id}' ({node.description}) has {len(leaves)} procedures directly in it.\n"
            f"Existing subcategories:\n{existing}\n\nProcedures:\n{listing}"),
            max_tokens=6000, schema=schemas.SOP_GROUPS, thinking=2048)
    except Exception:
        return []
    by_id = {s.id: s for s in leaves}
    plan = []
    for g in (r or {}).get("groups") or []:
        name = re.sub(r"[^a-z0-9_]", "_", str(g.get("name", "")).lower()).strip("_")
        if not name or name in by_id or name == node.id.split(".")[-1]:
            continue
        desc = str(g.get("description", "")).strip()[:300]
        confirmed = []
        for sid in g.get("members") or []:
            sop = by_id.get(sid)
            if sop is None or any(sop in m for _, _, _, m in plan):
                continue
            p = jev.yes(f"Does this procedure belong in the subcategory '{name}' of '{node.id}': {desc}?",
                        f"{sop.id}: {sop.description}", f"{name} {desc} {' '.join(g.get('keywords') or [])}",
                        "different unrelated other purpose",
                        yes_desc=f"Yes: it is one of the '{name}' procedures.", no_desc="No: it belongs elsewhere.")
            if p >= 0.5:
                confirmed.append(sop)
        if len(confirmed) >= (1 if name in subs else min_size):   # smaller would fold straight back
            plan.append((name, desc, [str(k) for k in (g.get("keywords") or [])][:8], confirmed))
    if not plan or (len(plan) == 1 and plan[0][0] not in subs and len(plan[0][3]) == len(leaves)):
        return []                                    # no real grouping: one group of everything is no split
    moved = []
    for name, desc, keywords, members in plan:
        sub_id = f"{node.id}.{name}"
        for sop in members:
            root = _root_of(lib, sop)
            if not (root.joinpath(*sub_id.split(".")) / "_node.json").exists() and name not in subs:
                _node_meta(root.joinpath(*sub_id.split(".")), desc, keywords)
            was_ok = ex is not None and sop.status == "validated" and not ex.test(sop.id)
            new = move(lib, root, sop, f"{sub_id}.{sop.id.split('.')[-1]}")
            if new is None:
                continue
            if was_ok and ex.test(new.id):           # a move must not break it: put it back
                move(lib, root, new, sop.id)
                continue
            moved.append(f"{sop.id} -> {new.id}")
    return moved


def crowded(root: Path, limit: int = LIMIT) -> dict[str, int]:
    """Categories under ``root`` with more than ``limit`` SOPs directly in them, read from the folders alone (no
    model, no code run): the cheap check a CI job makes before it rebalances anything."""
    out = {}
    for d in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]:
        if (d / "sop.json").exists() or any(part.startswith((".", "_")) for part in d.relative_to(root).parts):
            continue
        n = sum(1 for c in d.iterdir() if c.is_dir() and (c / "sop.json").exists())
        if n > limit and d != root:
            out[".".join(d.relative_to(root).parts)] = n
    return out


def rebalance_all(lib: Library, ex, llm, jev: Jev, limit: int = LIMIT, min_size: int = MIN_SIZE) -> list[dict]:
    """Every category of the library's private folders, deepest first (a split below can let a parent fold)."""
    cats = sorted({s.id.rsplit(".", 1)[0] for s in lib.sops.values() if "." in s.id and _root_of(lib, s)},
                  key=lambda c: -c.count("."))
    results, seen = [], set()
    for cat in cats:
        for c in [cat] + [".".join(cat.split(".")[:i]) for i in range(cat.count("."), 0, -1)]:
            if c and c not in seen:
                seen.add(c)
                r = rebalance(lib, ex, c, llm, jev, limit, min_size)
                if r["split"] or r["folded"]:
                    results.append(r)
    return results
