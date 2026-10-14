import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rameness.tools import Approver, Toolbox


class WebSearch(unittest.TestCase):
    def setUp(self):
        self.queries = []
        test = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = parse_qs(urlparse(self.path).query)
                test.queries.append(q)
                body = json.dumps({"results": [{"title": f"Result {i}", "url": f"https://example.com/{i}",
                                                "content": f"snippet {i}"} for i in range(12)]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()

    def test_results_come_from_the_configured_searxng_as_json(self):
        out = Toolbox(Path("."), Approver("auto"), None, search_url=self.url).handlers["web_search"](
            {"query": "rust async runtimes", "max_results": 3})
        self.assertEqual(self.queries[0], {"q": ["rust async runtimes"], "format": ["json"]})
        self.assertIn("1. Result 0\n   https://example.com/0\n   snippet 0", out)
        self.assertNotIn("Result 3", out)

    def test_without_a_search_server_there_is_no_search(self):
        out = Toolbox(Path("."), Approver("auto"), None).handlers["web_search"]({"query": "x"})
        self.assertIn("not configured", out)
        self.assertEqual(self.queries, [])

    def test_a_denied_search_is_not_sent(self):
        out = Toolbox(Path("."), Approver("readonly"), None, search_url=self.url).handlers["web_search"]({"query": "x"})
        self.assertTrue(out.startswith("DENIED"))
        self.assertEqual(self.queries, [])


if __name__ == "__main__":
    unittest.main()
