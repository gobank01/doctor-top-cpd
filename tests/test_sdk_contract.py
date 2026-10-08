"""Contract test: drive the REAL google-genai SDK (GeminiBackend) against a local
fake of generativelanguage.googleapis.com and assert the exact JSON it sends
matches the documented request shapes. Needs the venv:

  ~/.bizdrive/voice-env/bin/python3 -m unittest discover -s tests -v

Skipped automatically on a python without google-genai.
"""
from __future__ import annotations

import array
import base64
import io
import json
import sys
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voiceclone as vc  # noqa: E402

try:
    from google import genai  # noqa: F401
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


def _wav_bytes(seconds=0.5):
    pcm = array.array("h", [0] * int(24000 * seconds)).tobytes()
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    return b.getvalue()


class Fake(BaseHTTPRequestHandler):
    calls: list = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Fake.calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": None})
        self._reply(200, {"voices": [{"type": "replicated", "id": "voice_abc", "display_name": "Me",
                                      "model": "gemini-3.8-flash-tts", "expire_time": "2027-10-06T00:00:00Z"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Fake.calls.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
        if Fake.mode == "bad_key":
            return self._reply(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                               "message": "API key not valid. Please pass a valid API key.",
                                               "details": [{"reason": "API_KEY_INVALID"}]}})
        if Fake.mode == "consent_fail":
            return self._reply(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                               "message": "Consent audio verification failed."}})
        if self.path.split("?")[0].endswith("/voices"):
            return self._reply(200, {"type": "replicated", "id": "voice_abc123", "display_name": body["voice"].get("display_name"),
                                     "model": body["voice"].get("model"), "expire_time": "2027-10-06T00:00:00Z"})
        if self.path.split("?")[0].endswith("/interactions"):
            return self._reply(200, {
                "id": "interaction_1", "status": "completed", "model": body.get("model"),
                "created": "2026-10-06T00:00:00Z", "updated": "2026-10-06T00:00:00Z",
                "steps": [
                    {"type": "user_input", "content": body["input"][0]["content"]},
                    {"type": "model_output", "content": [
                        {"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(_wav_bytes()).decode()}]},
                ]})
        self._reply(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "no route"}})


@unittest.skipUnless(HAVE_SDK, "google-genai not installed (run with ~/.bizdrive/voice-env/bin/python3)")
class TestSdkContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.be = vc.GeminiBackend("TEST_KEY_NOT_REAL")
        cls.be.client = genai.Client(api_key="TEST_KEY_NOT_REAL", http_options={"base_url": base})

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        Fake.calls.clear()
        Fake.mode = "ok"

    def test_create_voice_request_shape(self):
        src, con = _wav_bytes(1), _wav_bytes(0.4)
        res = self.be.create_voice("เสียงของฉัน", src, con, "gemini-3.8-flash-tts")
        self.assertEqual(res["id"], "voice_abc123")
        self.assertTrue(res["expire_time"].startswith("2027-10-06"))
        call = Fake.calls[-1]
        self.assertEqual(call["path"].split("?")[0], "/v1beta/voices")
        self.assertEqual(call["headers"].get("x-goog-api-key"), "TEST_KEY_NOT_REAL")
        b = call["body"]
        self.assertIs(b["store"], True)
        v = b["voice"]
        self.assertEqual(v["type"], "replicated")
        self.assertEqual(v["model"], "gemini-3.8-flash-tts")
        self.assertEqual(v["display_name"], "เสียงของฉัน")
        self.assertEqual(v["replicated"]["source_audio"]["mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(v["replicated"]["source_audio"]["data"]), src)
        self.assertEqual(base64.b64decode(v["replicated"]["consent_audio"]["data"]), con)

    def test_synthesize_request_shape_and_audio(self):
        wav = self.be.synthesize("voice_abc123", "สวัสดีครับ <short pause> ทดสอบ", "warm and calm", "gemini-3.8-flash-tts")
        self.assertEqual(wav[:4], b"RIFF")
        call = Fake.calls[-1]
        self.assertEqual(call["path"].split("?")[0], "/v1beta/interactions")
        b = call["body"]
        self.assertEqual(b["model"], "gemini-3.8-flash-tts")
        self.assertEqual(b["response_format"], {"type": "audio"})
        self.assertEqual(b["generation_config"], {"speech_config": [{"voice": "voice_abc123"}]})
        self.assertEqual(b["input"], [{"type": "user_input", "content": [{
            "type": "text", "text": "สวัสดีครับ <short pause> ทดสอบ",
            "annotations": [{"type": "speech_metadata", "style": "warm and calm"}]}]}])

    def test_no_style_sends_no_annotations(self):
        self.be.synthesize("Kore", "hi", "", "gemini-3.8-flash-lite-tts")
        content = Fake.calls[-1]["body"]["input"][0]["content"][0]
        self.assertNotIn("annotations", content)
        self.assertEqual(Fake.calls[-1]["body"]["model"], "gemini-3.8-flash-lite-tts")

    def test_list_remote(self):
        voices = self.be.list_remote()
        self.assertEqual(voices[0]["id"], "voice_abc")
        self.assertIn("type=replicated", Fake.calls[-1]["path"])

    def test_api_error_is_mapped_to_thai(self):
        Fake.mode = "consent_fail"
        with self.assertRaises(vc.VoiceCloneError) as cm:
            self.be.create_voice("x", _wav_bytes(), _wav_bytes(), "gemini-3.8-flash-tts")
        self.assertEqual(cm.exception.code, "consent_mismatch")
        self.assertNotIn("TEST_KEY_NOT_REAL", cm.exception.detail)

    def test_bad_key_is_mapped(self):
        Fake.mode = "bad_key"
        with self.assertRaises(vc.VoiceCloneError) as cm:
            self.be.synthesize("Kore", "hi", "", "gemini-3.8-flash-tts")
        self.assertEqual(cm.exception.code, "bad_key")


if __name__ == "__main__":
    unittest.main()
