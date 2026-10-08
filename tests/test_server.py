"""HTTP-level tests for server.py in mock mode (temp data dir, no network).

  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import server  # noqa: E402
import voiceclone as vc  # noqa: E402
from test_voiceclone import make_wav  # noqa: E402


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.root = Path(cls._td.name)
        cls._env = mock.patch.dict(os.environ, {"VOICE_CLONE_MOCK": "1", "VOICE_CLONE_MOCK_TONE": "1"})
        cls._env.start()
        cls._paths = mock.patch.object(server, "PATHS", vc.Paths(root=cls.root / "data"))
        cls._paths.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls._paths.stop()
        cls._env.stop()
        cls._td.cleanup()

    def req(self, method, path, body=None, ctype="application/json", headers=None):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), raw
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def j(self, *a, **k):
        st, _, raw = self.req(*a, **k)
        return st, json.loads(raw)

    def test_full_flow(self):
        st, d = self.j("GET", "/api/status")
        self.assertEqual(st, 200)
        self.assertTrue(d["mock"])
        self.assertIn("ฉันเป็นเจ้าของเสียงนี้", d["consent_sentences"]["th-TH"])

        st, _, html = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("สตูดิโอโคลนเสียง".encode(), html)

        src = make_wav(self.root / "s.wav", 21).read_bytes()
        con = make_wav(self.root / "c.wav", 6).read_bytes()
        st, d = self.j("POST", "/api/sample?kind=source", src, "audio/wav")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"], d)
        st, d = self.j("POST", "/api/sample?kind=consent", con, "audio/wav")
        self.assertTrue(d["ok"], d)

        st, d = self.j("POST", "/api/voices", {"display_name": "เสียงของฉัน"})
        self.assertEqual((st, d["code"]), (400, "no_ack"))

        st, d = self.j("POST", "/api/voices", {"display_name": "เสียงของฉัน", "consent_ack": True})
        self.assertEqual(st, 200, d)
        vid = d["voice"]["id"]
        self.assertTrue(d["voice"]["source_wav"].startswith("samples/เสียงของฉัน-"))

        st, d = self.j("POST", "/api/speak", {"voice": vid, "text": "สวัสดีครับ", "style": "warm"})
        self.assertEqual(st, 200, d)
        mp3 = d["item"]["mp3"]
        url = "/files/" + "/".join(urllib.parse.quote(p) for p in mp3.split("/")) + "?download=1"
        st, hdrs, body = self.req("GET", url)
        self.assertEqual(st, 200)
        self.assertEqual(hdrs["Content-Type"], "audio/mpeg")
        self.assertIn("filename*=UTF-8''", hdrs["Content-Disposition"])
        self.assertGreater(len(body), 1000)

        st, d = self.j("GET", "/api/history")
        self.assertEqual(d["history"][0]["voice_id"], vid)
        st, d = self.j("GET", "/api/voices")
        self.assertTrue(d["voices"][0]["can_recreate"])

    def test_bad_requests(self):
        st, d = self.j("POST", "/api/sample?kind=bogus", b"x", "audio/wav")
        self.assertEqual(st, 400)
        st, d = self.j("POST", "/api/speak", {"voice": "Kore", "text": ""})
        self.assertEqual((st, d["code"]), (400, "no_text"))
        st, _, _ = self.req("GET", "/files/..%2F..%2Fserver.py")
        self.assertEqual(st, 404)
        st, d = self.j("POST", "/api/speak", {"voice": "Kore", "text": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)

    def test_missing_key_message_in_real_mode(self):
        with mock.patch.dict(os.environ, {"VOICE_CLONE_MOCK": ""}), mock.patch.object(vc, "get_api_key", lambda *a: ""):
            st, d = self.j("POST", "/api/speak", {"voice": "Kore", "text": "ทดสอบ"})
        self.assertEqual((st, d["code"]), (400, "missing_key"))
        self.assertIn(".env", d["error"])


if __name__ == "__main__":
    unittest.main()
