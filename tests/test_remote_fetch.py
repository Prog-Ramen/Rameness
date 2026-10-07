"""Fetching SOPs from a remote registry: level-by-level parallel search, revalidation, stragglers, verified files."""
import functools
import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rameness.jev import Jev, LexicalJev
from rameness.remote import RemoteRegistry

TREE = {"data": ["csv", "json"], "http": ["request", "download"], "git": ["commit", "branch"]}


def build(root: Path) -> Path:
    reg = root / "sops"
    reg.mkdir(parents=True)
    top = []
    for cat, subs in TREE.items():
        top.append({"type": "node", "id": cat, "description": f"{cat} procedures: {' '.join(subs)}",
                    "keywords": subs, "children": [f"{cat}.{s}" for s in subs]})
        cents = []
        for sub in subs:
            cents.append({"type": "node", "id": f"{cat}.{sub}", "description": f"{cat} {sub} procedures",
                          "keywords": [cat, sub]})
            sents = []
            for i in range(3):
                d = reg / cat / sub / f"op{i}"
                d.mkdir(parents=True)
                files = []
                for name, body in (("sop.json", json.dumps({"id": f"{cat}.{sub}.op{i}", "description": "x"})),
                                   ("run.py", "print('{}')\n")):
                    (d / name).write_text(body)
                    files.append({"path": f"{cat}/{sub}/op{i}/{name}",
                                  "sha256": hashlib.sha256(body.encode()).hexdigest()})
                sents.append({"type": "sop", "id": f"{cat}.{sub}.op{i}", "path": f"{cat}/{sub}/op{i}",
                              "description": f"{sub} {cat} operation {i}", "keywords": [cat, sub], "files": files})
            (reg / cat / sub / "_index.json").write_text(json.dumps({"entries": sents}))
        (reg / cat / "_index.json").write_text(json.dumps({"entries": cents}))
    (reg / "index.json").write_text(json.dumps({"entries": top}))
    return reg


class Handler(SimpleHTTPRequestHandler):
    delays: dict = {}             # path -> seconds
    seen: list = []

    def do_GET(self):
        Handler.seen.append((self.path, self.headers.get("If-Modified-Since") is not None))
        time.sleep(Handler.delays.get(self.path, 0.05))
        super().do_GET()

    def log_message(self, *a):
        pass


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.reg = build(self.tmp)
        Handler.delays, Handler.seen = {}, []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(self.reg)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/index.json"
        self.jev = Jev(LexicalJev())

    def tearDown(self):
        self.srv.shutdown()

    def search(self, r, task="parse a csv table of data"):
        return [s.id for s, _ in r.traverse(self.jev, task, "", {}, set(), 0.25, 0.15, 4)]

    def test_one_round_trip_per_tree_level(self):
        r = RemoteRegistry(self.url, self.tmp / "home")
        t0 = time.time()
        found = self.search(r)
        took = time.time() - t0
        self.assertTrue(found and all(s.startswith("data.csv.") for s in found), found)
        self.assertEqual(r.levels, 3)                                 # root, category, subcategory
        self.assertGreater(len(r.fetched), r.levels)                 # several listings per level...
        self.assertLess(took, 0.05 * len(r.fetched))                 # ...fetched in parallel, not one by one

    def test_a_stale_listing_is_revalidated_not_downloaded_again(self):
        self.search(RemoteRegistry(self.url, self.tmp / "home"))
        Handler.seen = []
        r = RemoteRegistry(self.url, self.tmp / "home", ttl=0)        # the cache has expired
        self.assertTrue(self.search(r))
        self.assertTrue(r.revalidated)
        self.assertEqual(r.fetched, [])                               # every listing came back 304
        self.assertTrue(all(conditional for _, conditional in Handler.seen))

    def test_a_straggler_does_not_hold_up_the_search(self):
        Handler.delays["/http/_index.json"] = 3.0
        r = RemoteRegistry(self.url, self.tmp / "home")
        t0 = time.time()
        got = r.listings(["data", "http", "git"], deadline=0.5)
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual(r.stragglers, ["http"])
        self.assertEqual(got["http"], [])                             # no cached copy yet: left out
        self.assertTrue(got["data"] and got["git"])

    def test_files_are_downloaded_in_parallel_and_verified(self):
        r = RemoteRegistry(self.url, self.tmp / "home")
        dest = r.fetch("data.csv.op1")
        self.assertTrue((dest / "run.py").exists() and (dest / "sop.json").exists())
        (self.reg / "data" / "csv" / "op2" / "run.py").write_text("print('tampered')\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            r.fetch("data.csv.op2")
        self.assertFalse((self.tmp / "home" / "public").joinpath(r.name, "sops", "data", "csv", "op2").exists())


if __name__ == "__main__":
    unittest.main()
