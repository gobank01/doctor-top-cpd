"""Cloud HTTP/storage contracts, with private Blob and Gemini replaced in memory.

No network, real API keys, existing voice records, or real voice samples are used.
Run with: .venv/bin/python -m unittest discover -s tests -p test_cloud.py -v
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import app as studio  # noqa: E402
import voiceclone as vc  # noqa: E402
from cloud_storage import CloudStore, PREFIX, validate_path  # noqa: E402
from test_voiceclone import make_wav  # noqa: E402


class MemoryBlobClient:
    """Implements the pinned Python Blob SDK response shapes, not local files."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.reads = []
        self.writes = []

    def get(self, key, *, access, use_cache):
        assert access == "private" and use_cache is False
        with self.lock:
            self.reads.append(key)
            raw = self.data.get(key)
            return None if raw is None else SimpleNamespace(status_code=200, content=raw)

    def put(self, key, raw, **options):
        assert options["access"] == "private"
        assert options["add_random_suffix"] is False
        with self.lock:
            if key in self.data and not options["overwrite"]:
                raise RuntimeError("Cannot overwrite existing blob")
            self.data[key] = bytes(raw)
            self.writes.append(key)

    def delete(self, key):
        with self.lock:
            self.data.pop(key, None)

    def list_objects(self, *, prefix, cursor=None, limit=1000):
        with self.lock:
            names = sorted(key for key in self.data if key.startswith(prefix))
        start = int(cursor or 0)
        end = start + limit
        return SimpleNamespace(
            blobs=[SimpleNamespace(pathname=key) for key in names[start:end]],
            has_more=end < len(names), cursor=str(end) if end < len(names) else None,
        )

    def close(self):
        pass


class TestSharedCloud(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cls.source = make_wav(root / "source.wav", 21).read_bytes()
            cls.other_source = make_wav(root / "other.wav", 22, amp=0.22).read_bytes()
            cls.consent = make_wav(root / "consent.wav", 6).read_bytes()

    def setUp(self):
        self.blobs = MemoryBlobClient()
        self.store = CloudStore(client=self.blobs)
        self.env = self.enterContext(mock.patch.dict(os.environ, {
            "VOICE_PASSWORD_HASH": "",
            "SESSION_SECRET": "unit-test-signing-secret-never-deployed",
            "VOICE_CLONE_MOCK": "1", "VOICE_CLONE_MOCK_TONE": "1",
        }))
        self.enterContext(mock.patch.object(studio, "get_store", return_value=self.store))
        self.enterContext(mock.patch.object(vc, "get_backend", return_value=vc.MockBackend()))
        self.enterContext(mock.patch.object(vc, "get_api_key", return_value=""))
        self.client = self.enterContext(TestClient(studio.app, base_url="https://studio.test"))

    def visitor_client(self):
        client = self.enterContext(TestClient(studio.app, base_url="https://studio.test"))
        response = client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def upload(self, client, kind, raw, *, uid="1" * 32, split=500_000):
        parts = [raw[i:i + split] for i in range(0, len(raw), split)]
        for index, part in enumerate(parts):
            response = client.post(
                f"/api/upload/chunk?upload_id={uid}&index={index}",
                content=part, headers={"content-type": "application/octet-stream"},
            )
            self.assertEqual(response.status_code, 200, response.text)
        response = client.post(f"/api/sample?kind={kind}", json={
            "upload_id": uid, "total_chunks": len(parts),
            "filename": "เสียงตัวอย่าง.wav", "content_type": "audio/wav",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"], response.json())
        return response.json()

    def create(self, client, name="เสียงทดสอบ", source=None):
        self.upload(client, "source", self.source if source is None else source)
        self.upload(client, "consent", self.consent)
        response = client.post("/api/voices", json={"display_name": name, "consent_ack": True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["voice"]

    def test_page_bootstraps_without_password_and_work_is_shared(self):
        self.store.put_bytes("out/shared.wav", b"shared studio audio")
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        token = self.client.cookies.get(studio.COOKIE)
        self.assertIsNotNone(studio.session_id(token))
        self.assertEqual(self.blobs.reads, [])
        self.assertFalse(self.client.get("/api/status").json()["password_required"])
        for path in ("/api/voices", "/api/history", "/api/status"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
            self.assertEqual(self.client.cookies.get(studio.COOKIE), token)
        other = self.visitor_client()
        self.assertNotEqual(other.cookies.get(studio.COOKIE), token)
        self.assertEqual(other.get("/files/out/shared.wav").content, b"shared studio audio")
        self.assertEqual(self.client.get("/.env").status_code, 404)
        self.assertEqual(self.client.get("/health").json(), {"ok": True, "service": "doctor-top-cpd"})

    def test_api_requests_never_create_conflicting_bootstrap_sessions(self):
        for path in ("/api/status", "/api/voices", "/api/voices/remote", "/api/history",
                     "/files/out/shared.wav", "/files/samples/_pending/source.wav"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["code"], "session_required")
                self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.post("/api/speak", json={}).status_code, 409)
        self.assertEqual(self.blobs.reads, [])
        self.assertEqual(self.blobs.writes, [])

    def test_anonymous_cookie_security_tamper_expiry_and_rotation(self):
        response = self.client.get("/")
        cookie_header = response.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(flag, cookie_header)
        token = self.client.cookies.get(studio.COOKIE)
        self.assertEqual(self.client.get("/api/voices").status_code, 200)
        self.upload(self.client, "source", self.source)
        self.client.cookies.clear()
        self.client.cookies.set(studio.COOKIE, token[:-1] + ("0" if token[-1] != "0" else "1"), domain="studio.test", path="/")
        self.assertEqual(self.client.get("/api/voices").status_code, 409)
        self.assertEqual(self.client.get("/").status_code, 200)
        rotated = self.client.cookies.get(studio.COOKIE)
        self.assertNotEqual(studio.session_id(rotated), studio.session_id(token))
        self.assertEqual(self.client.get("/api/status").json()["pending"], {})
        payload = base64.urlsafe_b64encode(json.dumps({"sid": "a" * 32, "exp": int(time.time()) - 1}).encode()).decode().rstrip("=")
        signature = hmac.new(os.environ["SESSION_SECRET"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.client.cookies.clear()
        self.client.cookies.set(studio.COOKIE, payload + "." + signature, domain="studio.test", path="/")
        self.assertEqual(self.client.get("/api/voices").status_code, 409)
        self.assertEqual(self.client.get("/").status_code, 200)
        fresh = self.client.cookies.get(studio.COOKIE)
        self.assertIsNotNone(studio.session_id(fresh))
        self.assertNotEqual(studio.session_id(fresh), "a" * 32)
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.assertNotEqual(self.client.cookies.get(studio.COOKIE), fresh)
        self.assertEqual(self.client.get("/api/voices").status_code, 200)

    def test_legacy_login_is_removed_and_missing_session_secret_fails_closed(self):
        response = self.client.get("/login", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        response = self.client.post("/api/login", json={"password": "unused"})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["code"], "password_login_removed")
        self.assertNotIn("set-cookie", response.headers)
        with mock.patch.dict(os.environ, {"SESSION_SECRET": ""}):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "session_unconfigured")
            self.assertNotIn("set-cookie", response.headers)
            self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.blobs.reads, [])

    def test_cross_origin_mutations_are_rejected_without_password(self):
        evil = {"origin": "https://other.test"}
        response = self.client.post("/api/login", json={"password": "test-owner-code"}, headers=evil)
        self.assertEqual(response.status_code, 403)
        client = self.visitor_client()
        response = client.post("/api/speak", json={"voice": "Kore", "text": "ทดสอบ"}, headers=evil)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.blobs.writes, [])
        response = client.post("/api/logout", headers={"origin": "https://studio.test"})
        self.assertEqual(response.status_code, 200)

    def test_provider_inventory_is_not_enumerated(self):
        client = self.visitor_client()
        with mock.patch.object(vc, "get_backend") as backend:
            response = client.get("/api/voices/remote")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "voices": [], "disabled": True})
        backend.assert_not_called()

    def test_other_installation_voice_ids_and_blob_records_are_rejected(self):
        foreign_id = "voice_original_other_installation"
        original_key = "voice-clone/voices/" + foreign_id + ".json"
        original = json.dumps({"id": foreign_id, "display_name": "Original studio voice"}).encode()
        self.blobs.data[original_key] = original
        client = self.visitor_client()
        self.assertEqual(client.get("/api/voices").json()["voices"], [])
        with mock.patch.object(vc, "get_backend") as backend:
            for voice in (foreign_id, "gemini:" + foreign_id, "unlisted-voice", "Original studio voice"):
                with self.subTest(voice=voice):
                    response = client.post("/api/speak", json={"voice": voice, "text": "ทดสอบ"})
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["code"], "unknown_voice")
        backend.assert_not_called()
        self.assertEqual(self.blobs.data, {original_key: original})
        self.assertNotIn(original_key, self.blobs.reads)

    def test_six_existing_prebuilt_choices_reach_provider(self):
        client = self.visitor_client()
        backend = mock.Mock()
        backend.synthesize.side_effect = vc.VoiceCloneError("mock provider unavailable", code="provider_unavailable", status=502)
        with mock.patch.object(vc, "get_backend", return_value=backend):
            for voice in ("Kore", "Puck", "Charon", "Achird", "Sulafat", "Vindemiatrix"):
                with self.subTest(voice=voice):
                    response = client.post("/api/speak", json={"voice": voice, "text": "ทดสอบ"})
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(response.json()["code"], "provider_unavailable")
                    self.assertEqual(backend.synthesize.call_args.args[0], voice)
        self.assertEqual(backend.synthesize.call_count, 6)
        self.assertEqual(self.blobs.writes, [])

    def test_upload_limits_and_incomplete_or_foreign_chunks(self):
        first, second = self.visitor_client(), self.visitor_client()
        path = "/api/upload/chunk?upload_id=" + "a" * 32
        for query, raw in (("&index=-1", b"a"), ("&index=20", b"a"), ("&index=0", b""),
                           ("&index=0", b"a" * (studio.CHUNK_SIZE + 1))):
            self.assertEqual(first.post(path + query, content=raw).status_code, 413)
        self.assertEqual(first.post("/api/upload/chunk?upload_id=../bad&index=0", content=b"a").status_code, 400)
        self.assertEqual(first.post(path + "&index=0", content=self.source).status_code, 200)
        data = {"upload_id": "a" * 32, "total_chunks": 1}
        self.assertEqual(second.post("/api/sample?kind=source", json=data).status_code, 400)
        data["total_chunks"] = 2
        self.assertEqual(first.post("/api/sample?kind=source", json=data).status_code, 400)
        data["total_chunks"] = True
        self.assertEqual(first.post("/api/sample?kind=source", json=data).status_code, 400)

    def test_pending_samples_are_isolated_between_sessions(self):
        first, second = self.visitor_client(), self.visitor_client()
        self.upload(first, "source", self.source)
        self.assertIn("source", first.get("/api/status").json()["pending"])
        self.assertEqual(second.get("/api/status").json()["pending"], {})
        self.assertEqual(first.get("/files/samples/_pending/source.wav").status_code, 200)
        self.assertEqual(second.get("/files/samples/_pending/source.wav").status_code, 404)
        self.assertEqual(second.post("/api/voices", json={"display_name": "ไม่มีเสียง", "consent_ack": True}).json()["code"], "missing_source")
        self.assertFalse(any("/uploads/" in key for key in self.blobs.data))

    def test_voice_creation_requires_explicit_acknowledgement(self):
        client = self.visitor_client()
        for value in (None, False, "true", 1):
            response = client.post("/api/voices", json={"display_name": "ทดสอบ", "consent_ack": value})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["code"], "no_ack")
        self.assertEqual(self.blobs.writes, [])

    def test_create_and_speak_survive_discarded_temporary_roots(self):
        client = self.visitor_client()
        real_temp = tempfile.TemporaryDirectory
        roots = []

        def track_temp(*args, **kwargs):
            value = real_temp(*args, **kwargs)
            roots.append(Path(value.name))
            return value

        with mock.patch.object(studio.tempfile, "TemporaryDirectory", side_effect=track_temp):
            voice = self.create(client)
            self.assertFalse(any(path.exists() for path in roots))
            fresh_client = self.visitor_client()
            self.assertEqual(fresh_client.get("/api/voices").json()["voices"][0]["id"], voice["id"])
            response = fresh_client.post("/api/speak", json={"voice": voice["id"], "text": "สวัสดีครับ ทดสอบเสียงบนคลาวด์", "style": "warm"})
            self.assertEqual(response.status_code, 200, response.text)
            item = response.json()["item"]
            self.assertFalse(any(path.exists() for path in roots))
        self.assertGreater(len(set(roots)), 3)
        self.assertEqual(client.get("/api/status").json()["pending"], {})
        for rel in (voice["source_wav"], voice["consent_wav"], item["wav"], item["mp3"]):
            response = fresh_client.get("/files/" + quote(rel, safe="/"))
            self.assertEqual(response.status_code, 200)
            self.assertGreater(len(response.content), 1000)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(fresh_client.get("/api/history").json()["history"][0]["voice_id"], voice["id"])

    def test_speech_history_does_not_rewrite_or_drop_voice_metadata(self):
        client = self.visitor_client()
        voice = self.create(client)
        second = {**voice, "id": "voice_mock_unrelated", "display_name": "อีกเสียง", "owner_note": "preserve"}
        self.store.put_json("voices/voice_mock_unrelated.json", second)
        originals = {key: raw for key, raw in self.blobs.data.items() if key.startswith(PREFIX + "voices/")}
        for text in ("ประโยคแรก", "ประโยคที่สอง"):
            response = client.post("/api/speak", json={"voice": voice["id"], "text": text})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(originals, {key: raw for key, raw in self.blobs.data.items() if key.startswith(PREFIX + "voices/")})
        self.assertEqual(len(client.get("/api/history").json()["history"]), 2)
        voices = {v["id"]: v for v in client.get("/api/voices").json()["voices"]}
        self.assertTrue(voices[voice["id"]]["last_used_at"])
        self.assertEqual(voices[second["id"]]["owner_note"], "preserve")

    def test_same_name_created_in_same_second_preserves_both_samples(self):
        client = self.visitor_client()
        fixed = vc.now_utc()
        with mock.patch.object(vc, "now_utc", return_value=fixed):
            first = self.create(client, name="ชื่อเดียวกัน", source=self.source)
            original = self.store.get_bytes(first["source_wav"])
            second = self.create(client, name="ชื่อเดียวกัน", source=self.other_source)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["source_wav"], second["source_wav"])
        self.assertEqual(self.store.get_bytes(first["source_wav"]), original)
        self.assertEqual(len(client.get("/api/voices").json()["voices"]), 2)

    def test_recreate_uses_durable_samples_and_keeps_original_voice(self):
        first = self.visitor_client()
        voice = self.create(first)
        originals = {key: self.store.get_bytes(voice[key]) for key in ("source_wav", "consent_wav")}
        fresh_client = self.visitor_client()
        response = fresh_client.post("/api/voices/recreate", json={"id": voice["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        recreated = response.json()["voice"]
        self.assertNotEqual(recreated["id"], voice["id"])
        listed = {v["id"]: v for v in fresh_client.get("/api/voices").json()["voices"]}
        self.assertEqual(listed[voice["id"]]["replaced_by"], recreated["id"])
        for key, raw in originals.items():
            self.assertEqual(self.store.get_bytes(voice[key]), raw)
            self.assertEqual(self.store.get_bytes(recreated[key]), raw)
        spoken = fresh_client.post("/api/speak", json={"voice": voice["id"], "text": "ใช้เสียงที่สร้างใหม่"})
        self.assertEqual(spoken.status_code, 200, spoken.text)
        self.assertEqual(spoken.json()["item"]["voice_id"], recreated["id"])

    def test_long_generated_audio_exceeds_platform_buffer_limit_and_streams(self):
        client = self.visitor_client()
        response = client.post("/api/speak", json={"voice": "Kore", "text": "เสียงทดสอบยาว " * 350})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["item"]
        raw = self.store.get_bytes(item["wav"])
        self.assertGreater(len(raw), 4.5 * 1024 * 1024)
        sizes = []

        async def observed_asgi(scope, receive, send):
            async def observed_send(message):
                if message["type"] == "http.response.body":
                    sizes.append(len(message.get("body", b"")))
                await send(message)
            await studio.app(scope, receive, observed_send)

        with TestClient(observed_asgi, base_url="https://studio.test") as reader:
            reader.cookies.update(client.cookies)
            media = reader.get("/files/" + quote(item["wav"], safe="/"))
        self.assertEqual(media.status_code, 200)
        self.assertEqual(media.content, raw)
        self.assertGreater(len(sizes), 2)
        self.assertLessEqual(max(sizes), 65536)
        self.assertNotIn("content-length", media.headers)
        self.assertEqual(client.get("/api/history").json()["history"][0]["id"], item["id"])

    def test_media_ranges_download_names_and_invalid_paths(self):
        client = self.visitor_client()
        raw = bytes(range(256)) * 100
        self.store.put_bytes("out/เสียงไทย.wav", raw, "audio/wav")
        url = "/files/" + quote("out/เสียงไทย.wav", safe="/")
        for header, expected, content_range in (
            ("bytes=10-19", raw[10:20], "bytes 10-19/25600"),
            ("bytes=-7", raw[-7:], "bytes 25593-25599/25600"),
            ("bytes=25590-", raw[25590:], "bytes 25590-25599/25600"),
            ("bytes=25590-99999", raw[25590:], "bytes 25590-25599/25600"),
        ):
            response = client.get(url, headers={"range": header})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, expected)
            self.assertEqual(response.headers["content-range"], content_range)
        for header in ("bytes=-", "bytes=9-1", "bytes=25600-", "bytes=0-1,4-5", "bytes=-0"):
            self.assertEqual(client.get(url, headers={"range": header}).status_code, 416)
        download = client.get(url + "?download=1")
        self.assertIn("filename*=UTF-8''" + quote("เสียงไทย.wav"), download.headers["content-disposition"])
        self.assertEqual(download.headers["accept-ranges"], "bytes")
        for path in ("out/..%2F..%2F.env", "out/a%5Cb.wav", "voices/a.json", "samples/_pending/not-source.wav", "out/app.py"):
            response = client.get("/files/" + path)
            self.assertEqual(response.status_code, 404, path)

    def test_invalid_json_size_and_provider_details_are_not_exposed(self):
        client = self.visitor_client()
        for body in (b"not json", b"[]", b"null"):
            self.assertEqual(client.post("/api/speak", content=body).status_code, 400)
        self.assertEqual(client.post("/api/speak", content=b"x" * (256 * 1024 + 1)).status_code, 413)
        with mock.patch.object(vc, "get_backend", side_effect=vc.VoiceCloneError("provider failed", code="provider", detail="private-request-material", status=502)):
            response = client.post("/api/speak", json={"voice": "Kore", "text": "ทดสอบ"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "")
        self.assertNotIn("private-request-material", response.text)


class TestCloudStorage(unittest.TestCase):
    def setUp(self):
        self.client = MemoryBlobClient()
        self.store = CloudStore(client=self.client)

    def test_private_utf8_round_trip_and_missing_default(self):
        record = {"display_name": "เสียงทดสอบ", "value": 42}
        self.store.put_json("voices/เสียง.json", record)
        self.assertEqual(self.store.get_json("voices/เสียง.json"), record)
        self.assertEqual(self.store.get_json("voices/missing.json", []), [])
        self.assertEqual(self.store.list_paths("voices/"), ["voices/เสียง.json"])
        self.assertEqual(self.client.writes, [PREFIX + "voices/เสียง.json"])

    def test_paths_cannot_escape_namespace(self):
        for path in ("", "/absolute", "../secret", "out/../secret", "out//file", "out/./file", "out\\file", "out/a\0.wav", "out/a\n.wav"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.put_bytes(path, b"x")
        self.assertEqual(self.client.writes, [])
        self.assertEqual(validate_path("samples/เสียงไทย.wav"), "samples/เสียงไทย.wav")

    def test_pagination_covers_every_object_once(self):
        for index in range(1003):
            self.store.put_bytes(f"out/{index:04}.wav", b"x")
        self.assertEqual(self.store.list_paths("out/"), [f"out/{index:04}.wav" for index in range(1003)])

    def test_pagination_and_prefix_anomalies_fail_closed(self):
        outside = SimpleNamespace(blobs=[SimpleNamespace(pathname="other-project/private.wav")], has_more=False, cursor=None)
        with mock.patch.object(self.client, "list_objects", return_value=outside), self.assertRaises(RuntimeError):
            self.store.list_paths("out/")
        repeated = SimpleNamespace(blobs=[], has_more=True, cursor="same")
        with mock.patch.object(self.client, "list_objects", return_value=repeated), self.assertRaises(RuntimeError):
            self.store.list_paths("out/")


if __name__ == "__main__":
    unittest.main()
