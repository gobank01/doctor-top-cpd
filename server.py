#!/usr/bin/env python3
"""server.py — Voice Clone local voice-clone web app (stdlib http.server).

  bash run.sh            # real API (needs GEMINI_API_KEY)
  bash run.sh --mock     # fake API, exercise the whole flow

Binds to 127.0.0.1 only. Endpoints (JSON):
  GET  /                         index.html
  GET  /api/status               key/sdk/ffmpeg/mock flags, pending clips, presets
  POST /api/sample?kind=source|consent   raw audio body → 24 kHz mono WAV + checks
  GET  /api/voices               voices.json
  POST /api/voices               {display_name, consent_lang, model, consent_ack:true}
  POST /api/voices/recreate      {id}
  GET  /api/voices/remote        replicated voices stored in the Google project
  POST /api/speak                {voice, text, style, model}
  GET  /api/history
  GET  /files/<path>             files under samples/ and out/
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voiceclone as vc  # noqa: E402

MAX_UPLOAD = 40 * 1024 * 1024
INDEX = vc.TOOL_DIR / "index.html"
PATHS = vc.DEFAULT_PATHS  # data root (voices.json, samples/, out/); tests swap this


def status_payload() -> dict:
    try:
        has_ffmpeg = bool(vc.ffmpeg_bin())
    except Exception:  # noqa: BLE001
        has_ffmpeg = False
    return {
        "ok": True,
        "mock": vc.is_mock(),
        "has_key": bool(vc.get_api_key()),
        "sdk": vc.sdk_available(),
        "ffmpeg": has_ffmpeg,
        "env_file": ".env",
        "project_dir": str(vc.TOOL_DIR),
        "default_model": vc.DEFAULT_MODEL,
        "models": vc.MODELS,
        "consent_sentences": vc.CONSENT_SENTENCES,
        "styles": {k: {"label": v[0], "style": v[1]} for k, v in vc.STYLE_PRESETS.items()},
        "limits": {"source_min": vc.SOURCE_MIN_S, "source_max": vc.SOURCE_MAX_S,
                   "consent_min": vc.CONSENT_MIN_S, "consent_max": vc.CONSENT_MAX_S,
                   "text_max": vc.TEXT_MAX_CHARS},
        "pending": vc.pending_status(PATHS),
    }


def voices_payload() -> dict:
    voices = vc.load_voices(PATHS)
    for v in voices:
        v["expired"] = vc.is_expired(v)
        root = PATHS.root
        v["can_recreate"] = bool(v.get("source_wav") and (root / v["source_wav"]).is_file()
                                 and v.get("consent_wav") and (root / v["consent_wav"]).is_file())
    voices.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    return {"ok": True, "voices": voices}


class Handler(BaseHTTPRequestHandler):
    server_version = "VoiceClone/1"

    def log_message(self, fmt, *args):  # short log, never bodies
        sys.stderr.write("[voice-clone] %s %s\n" % (self.command, self.path.split("?")[0]))

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _err(self, e: vc.VoiceCloneError):
        self._json(e.to_dict(), e.status or 400)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise vc.VoiceCloneError("ไฟล์ใหญ่เกินไป (เกิน 40 MB)", code="too_large", status=413)
        return self.rfile.read(n) if n > 0 else b""

    def _json_body(self) -> dict:
        raw = self._body()
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise vc.VoiceCloneError("JSON ไม่ถูกต้อง", code="bad_json")
        if not isinstance(data, dict):
            raise vc.VoiceCloneError("JSON ต้องเป็น object", code="bad_json")
        return data

    def _same_origin(self) -> bool:
        # Block cross-site POSTs from other web pages (CSRF). Browsers send Origin on POST.
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    # ---------------------------------------------------------------- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            u = urlparse(self.path)
            p = u.path
            if p in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if p == "/api/status":
                return self._json(status_payload())
            if p == "/api/voices":
                return self._json(voices_payload())
            if p == "/api/voices/remote":
                return self._json({"ok": True, "voices": vc.get_backend().list_remote()})
            if p == "/api/history":
                return self._json({"ok": True, "history": vc.load_history(PATHS)[:50]})
            if p.startswith("/files/"):
                return self._file(unquote(p[len("/files/"):]))
            return self._json({"ok": False, "error": "ไม่พบหน้า", "code": "not_found"}, 404)
        except vc.VoiceCloneError as e:
            return self._err(e)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": "เซิร์ฟเวอร์ผิดพลาด", "code": "internal", "detail": str(e)[:300]}, 500)

    def _file(self, rel: str):
        root = PATHS.root.resolve()
        target = (root / rel).resolve()
        allowed = [(root / "samples").resolve(), (root / "out").resolve()]
        if not any(target.is_relative_to(a) for a in allowed) or not target.is_file():
            return self._json({"ok": False, "error": "ไม่พบไฟล์", "code": "not_found"}, 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".mp3":
            ctype = "audio/mpeg"
        elif target.suffix == ".wav":
            ctype = "audio/wav"
        extra = {}
        if "download=1" in (urlparse(self.path).query or ""):
            ascii_name = "voice" + target.suffix
            extra["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(target.name)}"
        return self._send(200, target.read_bytes(), ctype, extra)

    def do_POST(self):
        try:
            if not self._same_origin():
                return self._json({"ok": False, "error": "ปฏิเสธคำขอจากเว็บอื่น", "code": "forbidden"}, 403)
            u = urlparse(self.path)
            p = u.path
            q = parse_qs(u.query)
            if p == "/api/sample":
                kind = (q.get("kind") or [""])[0]
                fname = (q.get("filename") or [""])[0]
                info = vc.prepare_clip(kind, self._body(), self.headers.get("Content-Type"), fname, paths=PATHS)
                return self._json(info)
            if p == "/api/voices":
                d = self._json_body()
                if d.get("consent_ack") is not True:
                    raise vc.VoiceCloneError(
                        "ต้องติ๊กยืนยันก่อนว่าเป็นเสียงของคุณเอง (หรือเจ้าของเสียงอัดประโยคยินยอมด้วยตัวเอง)",
                        code="no_ack")
                lang = d.get("consent_lang") or "th-TH"
                if lang not in vc.CONSENT_SENTENCES:
                    lang = "th-TH"
                rec = vc.create_voice(d.get("display_name", ""), lang, d.get("model") or vc.DEFAULT_MODEL, paths=PATHS)
                return self._json({"ok": True, "voice": rec})
            if p == "/api/voices/recreate":
                d = self._json_body()
                rec = vc.recreate_voice(str(d.get("id", "")), paths=PATHS)
                return self._json({"ok": True, "voice": rec})
            if p == "/api/speak":
                d = self._json_body()
                item = vc.speak(str(d.get("voice", "")), str(d.get("text", "")), str(d.get("style", "")),
                                str(d.get("model") or vc.DEFAULT_MODEL), paths=PATHS, record_history=True)
                return self._json({"ok": True, "item": item})
            return self._json({"ok": False, "error": "ไม่พบ endpoint", "code": "not_found"}, 404)
        except vc.VoiceCloneError as e:
            return self._err(e)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": "เซิร์ฟเวอร์ผิดพลาด", "code": "internal", "detail": str(e)[:300]}, 500)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    mode = "MOCK (ไม่เรียก API จริง)" if vc.is_mock() else "REAL (Gemini API)"
    print(f"[voice-clone] {mode} — เปิด http://localhost:{a.port}  (ข้อมูล: {PATHS.root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
