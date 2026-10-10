import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from rameness import jev as jev_mod
from rameness.jev import LexicalJev, Option, SystemOneJev, laya_running


class FakeLaya(BaseHTTPRequestHandler):
    """Speaks /v1/systemone like laya-serve: Noul says yes to options mentioning 'deploy';
    Choice picks the option mentioning 'deploy', with index-keyed probabilities if asked."""
    index_keys = False
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLaya.seen.append(body)
        answers = {}
        for k, q in body["questions"].items():
            if q["type"] == "noul":
                answers[k] = {"type": "noul", "noul": 0.9 if "deploy" in json.dumps(q) else 0.1}
            else:
                ids = list(q["criteria"])
                pr = [0.8 if "deploy" in q["criteria"][i] else 0.2 / (len(ids) - 1) for i in ids]
                keys = [str(n) for n in range(len(ids))] if FakeLaya.index_keys else ids
                answers[k] = {"type": "choice", "choice": ids[pr.index(max(pr))], "confidence": 0.8,
                              "probabilities": dict(zip(keys, pr))}
        out = json.dumps({"model": "laya-english", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *a):
        pass


DOWN = "http://127.0.0.1:9/v1/systemone"

OPTS = [Option("ship", "deploy the release"), Option("wait", "hold and do nothing"), Option("ask", "ask the user")]


class SystemOneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeLaya)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1/systemone"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeLaya.index_keys = False
        FakeLaya.seen = []

    def test_choose_is_one_choice_question(self):
        b = SystemOneJev("laya", url=self.url)
        p = b.choose("What next?", "the tests pass", OPTS)
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertEqual(max(range(3), key=p.__getitem__), 0)
        q = FakeLaya.seen[-1]
        self.assertEqual(q["model"], "typed-decisions")                   # the measured-best checkpoint
        self.assertEqual(q["questions"]["decision"]["criteria"]["ship"], "deploy the release")
        FakeLaya.index_keys = True                                  # servers that key probabilities by index
        self.assertEqual(b.choose("What next?", "x", OPTS), p)
        self.assertEqual(b.failures, 0)

    def test_models_read_option_sentences_and_the_log_keeps_them(self):
        import tempfile
        from pathlib import Path
        from rameness.jev import Jev
        from rameness.router import ROUTES
        log = Path(tempfile.mkdtemp()) / "decisions.jsonl"
        Jev(SystemOneJev("laya", url=self.url), log).choose("How?", "what is 418?", ROUTES, context="ctx")
        self.assertTrue(FakeLaya.seen[-1]["questions"]["decision"]["criteria"]["answer"].startswith("what why how"))
        Jev(SystemOneJev("laya", url=self.url, option_text="sentences")).choose("How?", "what is 418?", ROUTES)
        crit = FakeLaya.seen[-1]["questions"]["decision"]["criteria"]
        self.assertTrue(crit["answer"].startswith("Answer a question"))
        rec = json.loads(log.read_text())
        self.assertEqual(rec["option_descs"]["answer"], crit["answer"])       # the log keeps the sentence
        self.assertEqual(rec["context"], "ctx")

    def test_bench_runs_every_case(self):
        from rameness import jevbench
        from rameness.jev import Jev
        r = jevbench.run(Jev(SystemOneJev("laya", url=self.url)))
        self.assertEqual(r["n"], len(jevbench.CASES))
        self.assertEqual(set(r["sets"]), set(jevbench.SETS))
        self.assertEqual(jevbench.run(Jev(LexicalJev()), paraphrased=True)["n"], len(jevbench.PARAPHRASED))

    def test_activate_batches_noul_questions(self):
        b = SystemOneJev("laya", url=self.url)
        b.BATCH = 2
        p = b.activate("Which apply?", "ship it", OPTS, context="ctx")
        self.assertEqual(p, [0.9, 0.1, 0.1])
        self.assertEqual(len(FakeLaya.seen), 2)                     # 3 options in batches of 2
        self.assertEqual(FakeLaya.seen[0]["state"], "ship it\n\nctx")

    def test_state_fits_the_model_window_keeping_both_ends(self):
        from rameness.jev import fit, state_budget
        self.assertEqual(state_budget("laya", None), 1200)                   # 512-token base checkpoint
        self.assertEqual(state_budget("laya", "typed-decisions"), 3000)      # 1024-token checkpoints
        long = "Task: fix the parser\n" + "noise " * 2000 + "\nError: KeyError 'x'"
        b = SystemOneJev("laya", url=self.url, model="english")            # the 512-token checkpoint
        b.choose("What next?", long, OPTS)
        sent = FakeLaya.seen[-1]["state"]
        self.assertLessEqual(len(sent), 1200)
        self.assertTrue(sent.startswith("Task: fix the parser"))
        self.assertTrue(sent.endswith("Error: KeyError 'x'"))               # the error survives the cut
        self.assertEqual(fit("short", 100), "short")

    def test_sop_evaluation_reads_the_sop_code_only(self):
        import tempfile
        from pathlib import Path
        from rameness import publish
        from rameness.jev import Jev
        from rameness.org import Org
        from rameness.sops import Library, activate
        root = Path(tempfile.mkdtemp())
        d = root / "text" / "upper"
        d.mkdir(parents=True)
        (d / "sop.json").write_text(json.dumps({"id": "text.upper", "description": "Upper-case text"}))
        (d / "run.py").write_text("# a comment\n\nimport sys\nprint(sys.stdin.read().upper())\n")
        (root / "text" / "_node.json").write_text(json.dumps({"description": "Text utilities"}))
        sop = Library([(root, "private")]).get("text.upper")
        self.assertEqual(sop.digest(), "SOP text.upper: Upper-case text\nimport sys\nprint(sys.stdin.read().upper())")
        jev = Jev(SystemOneJev("laya", url=self.url))
        org = Org(name="Acme", facts=["Our warehouse is Snowflake"], private_terms=["zebra-cluster"])
        publish.classify(jev, sop, org, save=False)
        sent = FakeLaya.seen[-1]["state"]
        self.assertEqual(sent, sop.digest())                                 # no org profile, no private terms
        FakeLaya.seen = []
        activate(Library([(root, "private")]), jev, "make it upper case", org.render())
        self.assertTrue(FakeLaya.seen)
        self.assertTrue(all("Snowflake" not in json.dumps(q["state"]) for q in FakeLaya.seen))

    def test_laya_local_calls_predict_in_process_with_budgets(self):
        from rameness.jev import LayaLocalJev
        calls = []

        class FakeAgent:
            def predict(self, state, questions, max_len=None, head_max_len=None):
                calls.append((state, max_len, head_max_len))
                q = questions["decision"]
                return {"answers": {"decision": {"choice": "ship", "confidence": 0.7,
                                                 "probabilities": {k: (0.7 if k == "ship" else 0.15) for k in q["criteria"]}}}}

        LayaLocalJev._agents[("typed-decisions", "cpu")] = FakeAgent()
        try:
            b = LayaLocalJev("typed-decisions", device="cpu")
            self.assertEqual(b.max_state_chars, (1024 - 256) * 4)             # the checkpoint's trained window
            p = b.choose("What next?", "ship it", OPTS)
            self.assertEqual(max(range(3), key=p.__getitem__), 0)
            self.assertEqual(calls[-1], ("ship it", None, None))              # None = trained sizes
            LayaLocalJev._agents[("english", "cpu")] = FakeAgent()
            LayaLocalJev("english", max_len=2048, head_max_len=512, device="cpu").choose("q", "x", OPTS)
            self.assertEqual(calls[-1][1:], (2048, 512))
        finally:
            LayaLocalJev._agents.clear()

    def test_falls_back_when_server_is_down(self):
        b = SystemOneJev("laya", url="http://127.0.0.1:9/v1/systemone", timeout=0.5, fallback=LexicalJev())
        p = b.choose("What next?", "deploy the release now", OPTS)
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertEqual(b.failures, 1)
        self.assertTrue(b.last_error)

    def test_auto_prefers_laya_then_typesafe_then_lexical(self):
        self.assertTrue(laya_running(self.url))
        self.assertFalse(laya_running("http://127.0.0.1:9/v1/systemone"))
        self.assertEqual(jev_mod.resolve_backend({"backend": "auto", "laya_url": self.url, "clef_url": DOWN,
                                                  "kev_url": "http://127.0.0.1:9/v1/systemone"}), "laya")
        down = {"backend": "auto", "laya_url": "http://127.0.0.1:9/v1/systemone", "clef_url": DOWN,
                "kev_url": "http://127.0.0.1:9/v1/systemone"}
        # no local model at all: servers down and in-process Laya not installed
        no_laya = mock.patch.object(jev_mod, "laya_importable", return_value=False)
        no_laya.start()
        self.addCleanup(no_laya.stop)
        with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": "k"}):
            self.assertEqual(jev_mod.resolve_backend(down), "typesafe")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(jev_mod.resolve_backend(down), "lexical")
        no_laya.stop()
        cfg = {"jev": {"backend": "auto", "laya_url": self.url, "clef_url": DOWN,
                       "kev_url": "http://127.0.0.1:9/v1/systemone"}}
        self.assertEqual(jev_mod.build(cfg, llm=object()).backend.name, "laya")   # an LLM never decides
        with self.assertRaisesRegex(ValueError, "unknown jev.backend"):
            jev_mod.build({"jev": {"backend": "cascade"}})


class FakeClef(FakeLaya):
    """Like Clef's server: a request without a model name is refused."""

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        if "model" not in json.loads(body):
            self.send_response(400)
            self.end_headers()
            return
        import io
        self.rfile = io.BytesIO(body)
        self.headers.replace_header("Content-Length", str(length))
        FakeLaya.do_POST(self)


class ClefTest(unittest.TestCase):
    """Clef is a first-class backend: swap it in for Kev with one setting."""

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeClef)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1/systemone"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_the_readiness_ping_carries_the_model_name_clef_requires(self):
        self.assertFalse(laya_running(self.url))
        self.assertTrue(laya_running(self.url, model="clef-flash"))

    def test_backend_clef_sends_its_model_name_on_every_decision(self):
        FakeLaya.seen = []
        b = jev_mod.build({"jev": {"backend": "clef", "clef_url": self.url}}).backend
        self.assertEqual((b.name, b.model), ("clef", "clef-flash"))
        p = b.choose("What next?", "deploy the release now", OPTS)
        self.assertEqual(max(range(3), key=p.__getitem__), 0)
        self.assertEqual(FakeLaya.seen[-1]["model"], "clef-flash")

    def test_auto_finds_a_running_clef(self):
        self.assertEqual(jev_mod.resolve_backend({"backend": "auto", "clef_url": self.url, "kev_url": DOWN,
                                                  "laya_url": DOWN}), "clef")

    def test_jev_up_clef_runs_the_bundled_server_in_its_own_python(self):
        from rameness import jevserve
        cmd, _, _ = jevserve.command("clef", {"clef_python": "/opt/clef/bin/python", "clef_bits": 8})
        self.assertEqual(cmd[:2], ["/opt/clef/bin/python", "-I"])               # isolated: nothing shadows imports
        self.assertTrue(cmd[2].endswith("clef_serve.py"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "Cloudflare/clef-flash")
        self.assertEqual((cmd[cmd.index("--port") + 1], cmd[cmd.index("--bits") + 1]), ("8010", "8"))
        self.assertTrue(jevserve.installed("clef", {"clef_python": __file__}))


if __name__ == "__main__":
    unittest.main()
