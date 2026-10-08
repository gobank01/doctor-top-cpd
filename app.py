"""Doctor Top CPD shared studio with anonymous sessions and isolated Blob data."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import voiceclone as vc
from cloud_storage import CloudStore

ROOT = Path(__file__).resolve().parent
COOKIE = "doctor_top_cpd_session"
SESSION_SECONDS = 30 * 86400
CHUNK_SIZE = 2 * 1024 * 1024
MAX_UPLOAD = 40 * 1024 * 1024
PREBUILT_VOICES = frozenset(("Kore", "Puck", "Charon", "Achird", "Sulafat", "Vindemiatrix"))
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache(maxsize=1)
def get_store():
    return CloudStore()


def fail(message, code="bad_request", status=400):
    raise vc.VoiceCloneError(message, code=code, status=status)


def make_session():
    payload = base64.urlsafe_b64encode(json.dumps({"sid": secrets.token_hex(16), "exp": int(time.time()) + SESSION_SECONDS}).encode()).decode().rstrip("=")
    signature = hmac.new(os.environ["SESSION_SECRET"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def session_id(token):
    try:
        payload, signature = token.split(".")
        secret = os.environ.get("SESSION_SECRET", "")
        if not secret:
            return None
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["exp"] <= time.time() or not re.fullmatch(r"[a-f0-9]{32}", data["sid"]):
            return None
        return data["sid"]
    except (ValueError, KeyError, TypeError):
        return None


def set_session_cookie(response, request, token):
    response.set_cookie(
        COOKIE, token, httponly=True,
        secure=request.url.scheme == "https" or bool(os.environ.get("VERCEL")),
        samesite="strict", max_age=SESSION_SECONDS, path="/",
    )


def response_headers(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return response_headers(JSONResponse({"ok": False, "error": "ปฏิเสธคำขอจากเว็บอื่น"}, status_code=403))
    public = path in ("/login", "/api/login", "/health")
    sid = session_id(request.cookies.get(COOKIE, ""))
    new_token = None
    if not public and not sid:
        if path == "/" and request.method in ("GET", "HEAD"):
            if not os.environ.get("SESSION_SECRET"):
                return response_headers(JSONResponse(
                    {"ok": False, "error": "ยังตั้งค่าระบบสตูดิโอไม่ครบ", "code": "session_unconfigured"},
                    status_code=503,
                ))
            # Set the cookie on the HTML response before its parallel API reads.
            # API requests never create sessions: expired concurrent requests
            # must not save pending recordings under several different SIDs.
            new_token = make_session()
            sid = session_id(new_token)
        else:
            return response_headers(JSONResponse(
                {"ok": False, "error": "กรุณาเปิดหน้าสตูดิโอใหม่เพื่อเริ่มการใช้งาน", "code": "session_required"},
                status_code=409,
            ))
    request.state.sid = sid
    response = await call_next(request)
    if new_token:
        set_session_cookie(response, request, new_token)
    return response_headers(response)


@app.exception_handler(vc.VoiceCloneError)
async def voice_error(request, exc):
    body = exc.to_dict()
    body["detail"] = ""  # provider details can contain private request material
    return JSONResponse(body, status_code=exc.status)


@app.exception_handler(Exception)
async def internal_error(request, exc):
    logging.error("studio request failed: %s", type(exc).__name__)
    return JSONResponse({"ok": False, "error": "ระบบทำรายการไม่สำเร็จ กรุณาลองใหม่", "code": "internal"}, status_code=500)


async def json_body(request):
    raw = await request.body()
    if len(raw) > 256 * 1024:
        fail("ข้อมูลยาวเกินไป", status=413)
    try:
        value = json.loads(raw)
    except ValueError:
        fail("ข้อมูลไม่ถูกต้อง")
    if not isinstance(value, dict):
        fail("ข้อมูลไม่ถูกต้อง")
    return value


@app.get("/health")
def health():
    return {"ok": True, "service": "doctor-top-cpd"}


@app.get("/login")
def login_page():
    return RedirectResponse("/", status_code=303)


@app.post("/api/login")
def login():
    return JSONResponse(
        {"ok": False, "error": "สตูดิโอนี้เปิดใช้งานได้โดยไม่ต้องใช้รหัสผ่าน", "code": "password_login_removed"},
        status_code=410,
    )


@app.post("/api/logout")
def logout(request: Request):
    # Compatibility endpoint: starting a new anonymous session detaches this
    # browser from pending clips without imposing a login or hiding shared work.
    response = JSONResponse({"ok": True})
    set_session_cookie(response, request, make_session())
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html", media_type="text/html")


def records(store, prefix):
    keys = [p for p in store.list_paths(prefix) if p.endswith(".json")]
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(store.get_json, keys))
    return [v for v in values if isinstance(v, dict)]


def load_voices(store):
    voices = records(store, "voices/")
    # History is append-only. Derive last use without concurrent rewrites of a voice.
    latest = {}
    for item in records(store, "history/"):
        vid, when = item.get("voice_id"), item.get("created_at", "")
        latest[vid] = max(latest.get(vid, ""), when)
    for v in voices:
        when = latest.get(v["id"], "")
        if when and when > (v.get("last_used_at") or ""):
            v["last_used_at"] = when
            v["expires"] = vc.iso(vc.parse_iso(when) + vc.dt.timedelta(days=vc.STORED_TTL_DAYS))
    return sorted(voices, key=lambda v: v.get("created_at", ""))


def pending_key(sid, name):
    return f"sessions/{sid}/pending/{name}"


def pending_metadata(store, sid):
    out = {}
    for kind in ("source", "consent"):
        item = store.get_json(pending_key(sid, kind + ".json"))
        if item:
            out[kind] = item
    return out


@app.get("/api/status")
def status(request: Request):
    return {"ok": True, "cloud": True, "password_required": False, "mock": False, "has_key": bool(vc.get_api_key()),
            "sdk": vc.sdk_available(), "ffmpeg": bool(vc.ffmpeg_bin()), "env_file": "Vercel Environment Variables",
            "default_model": vc.DEFAULT_MODEL, "models": vc.MODELS, "consent_sentences": vc.CONSENT_SENTENCES,
            "styles": {k: {"label": v[0], "style": v[1]} for k, v in vc.STYLE_PRESETS.items()},
            "limits": {"source_min": vc.SOURCE_MIN_S, "source_max": vc.SOURCE_MAX_S,
                       "consent_min": vc.CONSENT_MIN_S, "consent_max": vc.CONSENT_MAX_S, "text_max": vc.TEXT_MAX_CHARS},
            "pending": pending_metadata(get_store(), request.state.sid)}


@app.get("/api/voices")
def voices():
    result = load_voices(get_store())
    for v in result:
        v["expired"] = vc.is_expired(v)
        v["can_recreate"] = bool(v.get("source_wav") and v.get("consent_wav"))
    return {"ok": True, "voices": list(reversed(result))}


@app.get("/api/voices/remote")
def remote_voices():
    # The owner authorized sharing the API key, not their other voice profiles.
    # Never enumerate the provider project from this independent studio.
    return {"ok": True, "voices": [], "disabled": True}


@app.get("/api/history")
def history():
    items = records(get_store(), "history/")
    return {"ok": True, "history": sorted(items, key=lambda v: v.get("created_at", ""), reverse=True)[:200]}


def validate_upload_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        fail("รหัสอัปโหลดไม่ถูกต้อง")
    return value


@app.post("/api/upload/chunk")
async def upload_chunk(request: Request, upload_id: str, index: int):
    uid = validate_upload_id(upload_id)
    if not 0 <= index < MAX_UPLOAD // CHUNK_SIZE:
        fail("ไฟล์ใหญ่เกิน 40 MB", status=413)
    raw = await request.body()
    if not 0 < len(raw) <= CHUNK_SIZE:
        fail("ขนาดส่วนไฟล์ไม่ถูกต้อง", status=413)
    key = f"sessions/{request.state.sid}/uploads/{uid}/{index}"
    await run_in_threadpool(get_store().put_bytes, key, raw)
    return {"ok": True, "index": index}


def process_sample(sid, kind, data):
    if kind not in ("source", "consent"):
        fail("ชนิดเสียงไม่ถูกต้อง")
    uid = validate_upload_id(data.get("upload_id"))
    count = data.get("total_chunks")
    if type(count) is not int or not 1 <= count <= MAX_UPLOAD // CHUNK_SIZE:
        fail("จำนวนส่วนไฟล์ไม่ถูกต้อง")
    store = get_store()
    keys = [f"sessions/{sid}/uploads/{uid}/{i}" for i in range(count)]
    parts = []
    for key in keys:
        part = store.get_bytes(key)
        if not part or len(part) > CHUNK_SIZE:
            fail("อัปโหลดไม่ครบ กรุณาอัปโหลดใหม่")
        parts.append(part)
    raw = b"".join(parts)
    if len(raw) > MAX_UPLOAD:
        fail("ไฟล์ใหญ่เกิน 40 MB", status=413)
    try:
        with tempfile.TemporaryDirectory(prefix="voice-sample-") as td:
            paths = vc.Paths(root=Path(td))
            result = vc.prepare_clip(kind, raw, str(data.get("content_type", ""))[:100], str(data.get("filename", ""))[:200], paths=paths)
            store.put_bytes(pending_key(sid, kind + ".wav"), (paths.pending / (kind + ".wav")).read_bytes(), "audio/wav")
            store.put_json(pending_key(sid, kind + ".json"), result)
            return result
    finally:
        for key in keys:
            store.delete(key)


@app.post("/api/sample")
async def sample(request: Request, kind: str):
    return await run_in_threadpool(process_sample, request.state.sid, kind, await json_body(request))


def hydrate_voice_records(store, paths):
    voices = load_voices(store)
    vc.save_voices(voices, paths)
    return voices


def restore_file(store, rel, root):
    if not rel or PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
        fail("ตำแหน่งไฟล์ไม่ถูกต้อง")
    raw = store.get_bytes(rel)
    if raw is None:
        fail("ไม่พบไฟล์เสียงเดิม", status=404)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)


def publish_voice(store, rec, paths):
    # Publish metadata last: listed voices always have both original clips stored.
    for key in ("source_wav", "consent_wav"):
        rel = rec[key]
        # Provider IDs keep simultaneous voices with the same display name apart.
        cloud_rel = f"samples/{rec['id']}/{Path(rel).name}"
        store.put_bytes(cloud_rel, (paths.root / rel).read_bytes(), "audio/wav")
        rec[key] = cloud_rel
    store.put_json(f"voices/{rec['id']}.json", rec)


def create_voice(sid, data):
    if data.get("consent_ack") is not True:
        fail("ต้องติ๊กยืนยันว่าเจ้าของเสียงยินยอมก่อน", "no_ack")
    store = get_store()
    with tempfile.TemporaryDirectory(prefix="voice-create-") as td:
        paths = vc.Paths(root=Path(td))
        paths.pending.mkdir(parents=True)
        for kind in ("source", "consent"):
            meta = store.get_json(pending_key(sid, kind + ".json"))
            raw = store.get_bytes(pending_key(sid, kind + ".wav"))
            if not meta or raw is None:
                fail("อัดเสียงทั้งสองขั้นก่อนสร้างเสียง", "missing_" + kind)
            (paths.pending / (kind + ".json")).write_text(json.dumps(meta))
            (paths.pending / (kind + ".wav")).write_bytes(raw)
        rec = vc.create_voice(str(data.get("display_name", "")), str(data.get("consent_lang") or "th-TH"), str(data.get("model") or vc.DEFAULT_MODEL), paths=paths)
        publish_voice(store, rec, paths)
        for kind in ("source", "consent"):
            for ext in (".wav", ".json"):
                store.delete(pending_key(sid, kind + ext))
        return {"ok": True, "voice": rec}


@app.post("/api/voices")
async def create(request: Request):
    return await run_in_threadpool(create_voice, request.state.sid, await json_body(request))


def recreate_voice(data):
    store = get_store()
    with tempfile.TemporaryDirectory(prefix="voice-recreate-") as td:
        paths = vc.Paths(root=Path(td))
        hydrate_voice_records(store, paths)
        old = vc.find_voice(str(data.get("id", "")), paths)
        if not old:
            fail("ไม่พบเสียงนี้", status=404)
        for key in ("source_wav", "consent_wav"):
            restore_file(store, old.get(key), paths.root)
        rec = vc.recreate_voice(old["id"], paths=paths)
        publish_voice(store, rec, paths)
        old["replaced_by"] = rec["id"]
        store.put_json(f"voices/{old['id']}.json", old)
        return {"ok": True, "voice": rec}


@app.post("/api/voices/recreate")
async def recreate(request: Request):
    return await run_in_threadpool(recreate_voice, await json_body(request))


def speak(data):
    store = get_store()
    with tempfile.TemporaryDirectory(prefix="voice-speak-") as td:
        paths = vc.Paths(root=Path(td))
        hydrate_voice_records(store, paths)
        voice = str(data.get("voice", ""))
        voice_id, record = vc.resolve_voice(voice, paths)
        if record is None and voice_id not in PREBUILT_VOICES:
            fail("ไม่พบเสียงนี้ในสตูดิโอ กรุณาเลือกเสียงที่บันทึกไว้หรือเสียงสำเร็จรูป", "unknown_voice", status=404)
        item = vc.speak(voice, str(data.get("text", "")), str(data.get("style", "")), str(data.get("model") or vc.DEFAULT_MODEL), paths=paths)
        for kind in ("wav", "mp3"):
            rel = item[kind]
            store.put_bytes(rel, (paths.root / rel).read_bytes(), "audio/wav" if kind == "wav" else "audio/mpeg")
        store.put_json(f"history/{item['id']}.json", item)
        return {"ok": True, "item": item}


@app.post("/api/speak")
async def synthesize(request: Request):
    return await run_in_threadpool(speak, await json_body(request))


@app.get("/files/{rel:path}")
def media(request: Request, rel: str):
    parts = PurePosixPath(rel).parts
    if not parts or parts[0] not in ("samples", "out") or ".." in parts or "\\" in rel or rel.startswith("/") or Path(rel).suffix.lower() not in (".wav", ".mp3"):
        fail("ไม่พบไฟล์", status=404)
    key = rel
    if rel.startswith("samples/_pending/"):
        name = rel[len("samples/_pending/"):]
        if name not in ("source.wav", "consent.wav"):
            fail("ไม่พบไฟล์", status=404)
        key = pending_key(request.state.sid, name)
    raw = get_store().get_bytes(key)
    if raw is None:
        fail("ไม่พบไฟล์", status=404)
    total = len(raw)
    headers = {"Accept-Ranges": "bytes"}
    code = 200
    range_header = request.headers.get("range")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        if not match or (not match[1] and not match[2]):
            return JSONResponse({"ok": False}, status_code=416, headers={"Content-Range": f"bytes */{total}"})
        start = int(match[1]) if match[1] else max(0, total - int(match[2]))
        end = min(int(match[2]), total - 1) if match[1] and match[2] else total - 1
        if start > end or start >= total:
            return JSONResponse({"ok": False}, status_code=416, headers={"Content-Range": f"bytes */{total}"})
        raw = raw[start:end + 1]
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        code = 206
    if request.query_params.get("download") == "1":
        headers["Content-Disposition"] = f"attachment; filename=voice{Path(rel).suffix}; filename*=UTF-8''{quote(Path(rel).name)}"
    ctype = "audio/mpeg" if rel.endswith(".mp3") else "audio/wav"
    return StreamingResponse((raw[i:i + 65536] for i in range(0, len(raw), 65536)), media_type=ctype, status_code=code, headers=headers)
