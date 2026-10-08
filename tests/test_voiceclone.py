"""Unit tests for Voice Clone (no network, no API key, plain python3).

  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import array
import base64
import datetime as dt
import io
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voiceclone as vc  # noqa: E402


def make_wav(path: Path, seconds: float, rate: int = 24000, amp: float = 0.3, noise: float = 0.001,
             pauses: bool = True, channels: int = 1, clip: bool = False) -> Path:
    """Speech-like test signal: 300 ms tone bursts with 150 ms gaps + a little noise."""
    rnd = random.Random(1)
    n = int(seconds * rate)
    out = array.array("h")
    for i in range(n):
        t = i / rate
        on = (not pauses) or (t % 0.45) < 0.30
        s = (amp * math.sin(2 * math.pi * 180 * t) * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * t))) if on else 0.0
        if clip and on:
            s = 1.5 * math.copysign(1, s) if s else 0
        s += noise * (rnd.random() * 2 - 1)
        v = int(max(-1.0, min(1.0, s)) * 32767)
        for _ in range(channels):
            out.append(v)
    if sys.byteorder == "big":
        out.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    return path


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.paths = vc.Paths(root=self.root)

    def tearDown(self):
        self._td.cleanup()


class TestConversion(TmpCase):
    def test_48k_stereo_wav_becomes_24k_mono_16bit(self):
        src = make_wav(self.root / "in.wav", 3, rate=48000, channels=2)
        dst = self.root / "out.wav"
        vc.convert_to_wav(src, dst)
        with wave.open(str(dst), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (24000, 1, 2))
            self.assertAlmostEqual(w.getnframes() / 24000, 3.0, delta=0.05)

    def test_compressed_upload_is_converted(self):
        src = make_wav(self.root / "in.wav", 2, rate=44100)
        m4a = self.root / "in.m4a"
        subprocess.run([vc.ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-c:a", "aac", str(m4a)], check=True)
        info = vc.prepare_clip("consent", m4a.read_bytes(), "audio/mp4", "x.m4a", paths=self.paths)
        with wave.open(str(self.root / info["wav"]), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels()), (24000, 1))

    def test_garbage_bytes_give_thai_error(self):
        with self.assertRaises(vc.VoiceCloneError) as cm:
            vc.prepare_clip("source", b"not audio at all" * 10, "audio/webm", paths=self.paths)
        self.assertEqual(cm.exception.code, "bad_audio")

    def test_ext_for(self):
        self.assertEqual(vc.ext_for("audio/webm;codecs=opus"), ".webm")
        self.assertEqual(vc.ext_for("audio/mp4"), ".m4a")
        self.assertEqual(vc.ext_for("", "voice.MP3"), ".mp3")
        self.assertEqual(vc.ext_for("application/octet-stream", "../../etc/passwd"), ".bin")


class TestChecks(TmpCase):
    def prep(self, kind, wav):
        return vc.prepare_clip(kind, wav.read_bytes(), "audio/wav", paths=self.paths)

    def test_good_source_passes(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 22))
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["errors"], [])
        self.assertAlmostEqual(info["metrics"]["duration"], 22, delta=0.1)
        self.assertTrue((self.paths.pending / "source.wav").exists())
        self.assertIn("source", vc.pending_status(self.paths))

    def test_short_source_fails(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 5))
        self.assertFalse(info["ok"])
        self.assertIn("สั้นเกินไป", info["errors"][0])

    def test_12s_source_warns_but_passes(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 12))
        self.assertTrue(info["ok"])
        self.assertTrue(any("20–30" in w for w in info["warnings"]))

    def test_long_source_trimmed_to_30s(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 40))
        self.assertTrue(info["ok"], info)
        self.assertAlmostEqual(info["metrics"]["duration"], 30.0, delta=0.05)
        self.assertTrue(any("ตัดเหลือ 30" in w for w in info["warnings"]))

    def test_silence_is_too_quiet(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 20, amp=0.0, noise=0.0005))
        self.assertFalse(info["ok"])
        self.assertTrue(any("เบามาก" in e for e in info["errors"]), info["errors"])

    def test_noise_only_is_rejected(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 20, amp=0.0, noise=0.3))
        self.assertFalse(info["ok"])
        self.assertTrue(any("รบกวน" in e for e in info["errors"]), info["errors"])

    def test_clipping_warns(self):
        info = self.prep("source", make_wav(self.root / "a.wav", 20, clip=True))
        self.assertTrue(any("เสียงแตก" in w for w in info["warnings"]), info)

    def test_consent_length_rules(self):
        self.assertTrue(self.prep("consent", make_wav(self.root / "c.wav", 6))["ok"])
        self.assertFalse(self.prep("consent", make_wav(self.root / "c.wav", 1))["ok"])
        long = self.prep("consent", make_wav(self.root / "c.wav", 25))
        self.assertFalse(long["ok"])
        self.assertIn("ยาวเกินไป", long["errors"][0])

    def test_bad_kind(self):
        with self.assertRaises(vc.VoiceCloneError):
            vc.prepare_clip("other", b"x", paths=self.paths)


class TestVoicesJson(TmpCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(vc.load_voices(self.paths), [])

    def test_upsert_find_touch(self):
        rec = {"id": "voice_abc", "display_name": "เสียงของฉัน", "created_at": "2026-10-06T00:00:00Z",
               "expires": "2027-10-06T00:00:00Z", "store": True}
        vc.upsert_voice(rec, self.paths)
        vc.upsert_voice(dict(rec, display_name="เสียงของฉัน"), self.paths)  # same id → replaced, not duplicated
        self.assertEqual(len(vc.load_voices(self.paths)), 1)
        self.assertEqual(vc.find_voice("voice_abc", self.paths)["id"], "voice_abc")
        self.assertEqual(vc.find_voice("เสียงของฉัน", self.paths)["id"], "voice_abc")
        self.assertIsNone(vc.find_voice("nobody", self.paths))
        raw = self.paths.voices_json.read_text(encoding="utf-8")
        self.assertIn("เสียงของฉัน", raw)  # Thai kept readable (ensure_ascii=False)
        when = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
        vc.touch_voice("voice_abc", when, self.paths)
        v = vc.find_voice("voice_abc", self.paths)
        self.assertEqual(v["last_used_at"], "2026-12-01T00:00:00Z")
        self.assertEqual(v["expires"], "2027-12-01T00:00:00Z")

    def test_name_lookup_is_case_insensitive_and_newest_wins(self):
        vc.upsert_voice({"id": "voice_old", "display_name": "Bank"}, self.paths)
        vc.upsert_voice({"id": "voice_new", "display_name": "bank"}, self.paths)
        self.assertEqual(vc.find_voice("BANK", self.paths)["id"], "voice_new")

    def test_corrupt_file_raises_instead_of_wiping(self):
        self.paths.voices_json.write_text("{broken", encoding="utf-8")
        with self.assertRaises(vc.VoiceCloneError) as cm:
            vc.load_voices(self.paths)
        self.assertEqual(cm.exception.code, "voices_corrupt")
        self.assertEqual(self.paths.voices_json.read_text(encoding="utf-8"), "{broken")

    def test_expiry(self):
        past = {"expires": "2020-01-01T00:00:00Z"}
        future = {"expires": "2999-01-01T00:00:00Z"}
        self.assertTrue(vc.is_expired(past))
        self.assertFalse(vc.is_expired(future))
        self.assertFalse(vc.is_expired({}))

    def test_resolve_voice_passthrough_and_prefix(self):
        vc.upsert_voice({"id": "voice_abc", "display_name": "Me"}, self.paths)
        self.assertEqual(vc.resolve_voice("gemini:voice_abc", self.paths)[0], "voice_abc")
        self.assertEqual(vc.resolve_voice("Me", self.paths)[0], "voice_abc")
        self.assertEqual(vc.resolve_voice("Kore", self.paths), ("Kore", None))
        vc.upsert_voice({"id": "voice_abc", "display_name": "Me", "replaced_by": "voice_def"}, self.paths)
        vc.upsert_voice({"id": "voice_def", "display_name": "Me"}, self.paths)
        self.assertEqual(vc.resolve_voice("voice_abc", self.paths)[0], "voice_def")


class TestSlug(unittest.TestCase):
    def test_thai_marks_kept_and_unsafe_chars_dropped(self):
        self.assertEqual(vc._slug("เสียงของฉัน"), "เสียงของฉัน")
        self.assertEqual(vc._slug("../../etc"), "etc")
        self.assertEqual(vc._slug("Bank Voice #2"), "Bank-Voice-2")
        self.assertEqual(vc._slug("   "), "voice")


class TestText(unittest.TestCase):
    def test_short_text_one_chunk(self):
        self.assertEqual(vc.chunk_text("  สวัสดีครับ  "), ["สวัสดีครับ"])
        self.assertEqual(vc.chunk_text(""), [])

    def test_paragraphs_grouped_under_limit(self):
        paras = [("ก" * 40) + f" ย่อหน้า {i}" for i in range(30)]
        text = "\n\n".join(paras)
        chunks = vc.chunk_text(text, max_chars=200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", "").replace(" ", ""), text.replace("\n", "").replace(" ", ""))

    def test_long_paragraph_split_at_spaces_or_hard(self):
        words = " ".join(["คำ" + str(i) for i in range(400)])
        chunks = vc.chunk_text(words, max_chars=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual(" ".join(chunks).split(), words.split())
        nospace = "ก" * 1000
        chunks = vc.chunk_text(nospace, max_chars=300)
        self.assertEqual("".join(chunks), nospace)
        self.assertTrue(all(len(c) <= 300 for c in chunks))


class TestAudioBytes(unittest.TestCase):
    def test_wav_and_raw_l16(self):
        pcm = array.array("h", [0, 1000, -1000, 0] * 100).tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
        self.assertEqual(vc.audio_bytes_to_pcm(buf.getvalue()), (pcm, 24000))
        self.assertEqual(vc.audio_bytes_to_pcm(pcm), (pcm, 24000))

    def test_extract_audio_shapes(self):
        wavb = b"RIFF....WAVEfmt "
        b64 = base64.b64encode(wavb).decode()
        # SDK convenience property (object)
        obj = type("I", (), {"output_audio": type("A", (), {"data": b64})()})()
        self.assertEqual(vc.extract_audio(obj), wavb)
        # raw REST JSON (steps[].content[])
        rest = {"steps": [{"type": "user_input", "content": [{"type": "text", "text": "x"}]},
                          {"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": b64}]}]}
        self.assertEqual(vc.extract_audio(rest), wavb)
        with self.assertRaises(vc.VoiceCloneError):
            vc.extract_audio({"steps": []})


class FakeErr(Exception):
    def __init__(self, code, status, message):
        super().__init__("API error occurred")
        self.status_code = code
        self.body = json.dumps({"error": {"code": code, "status": status, "message": message}})


class TestErrors(unittest.TestCase):
    def test_mapping(self):
        cases = [
            (FakeErr(400, "INVALID_ARGUMENT", "Consent audio does not match the required statement"), "consent_mismatch"),
            (FakeErr(400, "INVALID_ARGUMENT", "Speaker verification failed"), "consent_mismatch"),
            (FakeErr(429, "RESOURCE_EXHAUSTED", "Quota exceeded for metric"), "quota"),
            (FakeErr(400, "API_KEY_INVALID", "API key not valid. Please pass a valid API key."), "bad_key"),
            (FakeErr(404, "NOT_FOUND", "Voice voice_x not found"), "voice_not_found"),
            (FakeErr(500, "INTERNAL", "boom"), "server"),
            (FakeErr(400, "INVALID_ARGUMENT", "source audio duration too short"), "bad_sample"),
        ]
        for exc, code in cases:
            with self.subTest(code=code):
                e = vc.map_api_error(exc)
                self.assertEqual(e.code, code, e.detail)
                self.assertTrue(e.message)

    def test_dict_body_from_sdk_compat_errors(self):
        class Compat(Exception):
            status_code = 429
            body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
        self.assertEqual(vc.map_api_error(Compat("Error code: 429")).code, "quota")

        class Conn(Exception):
            pass
        Conn.__name__ = "APIConnectionError"
        self.assertEqual(vc.map_api_error(Conn("Connection error.")).code, "network")

    def test_key_never_leaks(self):
        key = "unit-test-api-key-not-real"
        e = vc.map_api_error(FakeErr(400, "INVALID_ARGUMENT", f"bad request for key {key}"), api_key=key)
        self.assertNotIn(key, e.detail)
        self.assertNotIn(key, e.message)

    def test_missing_key(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("ELEVENLABS_API_KEY=x\nGEMINI_API_KEY=\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "VOICE_CLONE_MOCK": ""}), \
                 mock.patch.object(vc, "ENV_FILE", env):
                self.assertEqual(vc.get_api_key(env), "")
                with mock.patch.object(vc, "get_api_key", lambda: ""):
                    with self.assertRaises(vc.VoiceCloneError) as cm:
                        vc.get_backend()
                    self.assertEqual(cm.exception.code, "missing_key")
                    self.assertIn("GEMINI_API_KEY", cm.exception.message)

    def test_env_parsing(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text('# c\nexport GEMINI_API_KEY="  abc123  "\nOTHER=1 # note\n', encoding="utf-8")
            vals = vc.read_env_file(env)
            self.assertEqual(vals["GEMINI_API_KEY"], "abc123")
            self.assertEqual(vals["OTHER"], "1")
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
                self.assertEqual(vc.get_api_key(env), "abc123")


class TestMockFlow(TmpCase):
    def test_create_speak_recreate_end_to_end(self):
        with mock.patch.dict(os.environ, {"VOICE_CLONE_MOCK": "1", "VOICE_CLONE_MOCK_TONE": "1"}):  # tone = fast
            be = vc.MockBackend()
            vc.prepare_clip("source", make_wav(self.root / "s.wav", 21).read_bytes(), "audio/wav", paths=self.paths)
            vc.prepare_clip("consent", make_wav(self.root / "c.wav", 6).read_bytes(), "audio/wav", paths=self.paths)
            rec = vc.create_voice("เสียงทดสอบ", backend=be, paths=self.paths)
            self.assertTrue(rec["id"].startswith("voice_mock_"))
            self.assertTrue((self.root / rec["source_wav"]).is_file())
            self.assertTrue((self.root / rec["consent_wav"]).is_file())
            self.assertEqual(vc.pending_status(self.paths), {})  # pending clips moved into the voice folder
            item = vc.speak("เสียงทดสอบ", "สวัสดี\n\nนี่คือการทดสอบ", style="warm", backend=be,
                            paths=self.paths, record_history=True)
            self.assertTrue((self.root / item["mp3"]).is_file())
            self.assertTrue((self.root / item["wav"]).is_file())
            self.assertGreater(item["duration"], 0.5)
            self.assertEqual(vc.load_history(self.paths)[0]["id"], item["id"])
            self.assertIsNotNone(vc.find_voice(rec["id"], self.paths)["last_used_at"])
            out = self.root / "cli.mp3"
            item2 = vc.speak(rec["id"], "ทดสอบ", backend=be, paths=self.paths, out_path=out)
            self.assertTrue(out.is_file())
            self.assertIsNone(item2["wav"])  # mp3-only output when --out x.mp3
            new = vc.recreate_voice(rec["id"], backend=be, paths=self.paths)
            self.assertNotEqual(new["id"], rec["id"])
            self.assertEqual(vc.resolve_voice(rec["id"], self.paths)[0], new["id"])

    def test_create_requires_both_passing_clips(self):
        be = vc.MockBackend()
        with self.assertRaises(vc.VoiceCloneError) as cm:
            vc.create_voice("x", backend=be, paths=self.paths)
        self.assertEqual(cm.exception.code, "missing_source")
        vc.prepare_clip("source", make_wav(self.root / "s.wav", 4).read_bytes(), "audio/wav", paths=self.paths)
        vc.prepare_clip("consent", make_wav(self.root / "c.wav", 6).read_bytes(), "audio/wav", paths=self.paths)
        with self.assertRaises(vc.VoiceCloneError) as cm:
            vc.create_voice("x", backend=be, paths=self.paths)
        self.assertEqual(cm.exception.code, "bad_source")

    def test_mock_error_triggers(self):
        be = vc.MockBackend()
        with self.assertRaises(vc.VoiceCloneError) as cm:
            be.create_voice("mock-consent-fail", b"", b"", vc.DEFAULT_MODEL)
        self.assertEqual(cm.exception.code, "consent_mismatch")
        with self.assertRaises(vc.VoiceCloneError) as cm:
            be.synthesize("voice_mock_x", "hi [[quota]]", "", vc.DEFAULT_MODEL)
        self.assertEqual(cm.exception.code, "quota")

    def test_mock_voice_blocked_in_real_mode(self):
        vc.upsert_voice({"id": "voice_mock_1", "display_name": "m", "mock": True}, self.paths)
        with mock.patch.dict(os.environ, {"VOICE_CLONE_MOCK": ""}):
            with self.assertRaises(vc.VoiceCloneError) as cm:
                vc.speak("voice_mock_1", "x", backend=vc.MockBackend(), paths=self.paths)
            self.assertEqual(cm.exception.code, "mock_voice")


if __name__ == "__main__":
    unittest.main()
