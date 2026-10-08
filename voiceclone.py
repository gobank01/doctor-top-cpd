#!/usr/bin/env python3
"""Core of Voice Clone — voice clone (Gemini 3.8 Flash TTS "replicated" voices).

Shared by server.py (the local web app) and speak.py (the standalone CLI). Standard library only at import time; the google-genai SDK is
imported lazily so mock mode and the unit tests run on plain python3.

Official API shapes used (verified 2026-10-06 against
https://ai.google.dev/gemini-api/docs/voice-replication and google-genai 2.28.0):

  client.voices.create(store=True, voice={
      "model": "gemini-3.8-flash-tts", "type": "replicated", "display_name": ...,
      "replicated": {"source_audio": {"mime_type": "audio/wav", "data": <b64>},
                     "consent_audio": {"mime_type": "audio/wav", "data": <b64>}}})
  -> Voice(id="voice_...", expire_time=...)

  client.interactions.create(model=..., input=[{"type": "user_input", "content": [
      {"type": "text", "text": ..., "annotations": [{"type": "speech_metadata",
       "style": ...}]}]}], response_format={"type": "audio"},
      generation_config={"speech_config": [{"voice": "voice_..."}]})
  -> interaction.output_audio.data = base64 WAV (24 kHz mono 16-bit, RIFF header)
"""
from __future__ import annotations

import array
import base64
import datetime as dt
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
ENV_FILE = TOOL_DIR / ".env"
VENV_PYTHON = TOOL_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

DEFAULT_MODEL = "gemini-3.8-flash-tts"
MODELS = {
    "gemini-3.8-flash-tts": "Flash TTS (คุณภาพสูงสุด แนะนำ)",
    "gemini-3.8-flash-lite-tts": "Flash-Lite TTS (ยังไม่ระบุรองรับภาษาไทย)",
}
SAMPLE_RATE = 24000
STORED_TTL_DAYS = 365  # store=True voices: 1 year from last use

# Verbatim consent statements (docs: "Supported consent phrases by language").
# Read EXACTLY as written — keep "ฉัน" even for a male speaker.
CONSENT_SENTENCES = {
    "th-TH": "ฉันเป็นเจ้าของเสียงนี้ และฉันยินยอมให้ Google ใช้เสียงนี้เพื่อสร้างแบบจำลองเสียงสังเคราะห์",
    "en-US": "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
}

# Turn-level delivery goes in speech_metadata.style (English works best per the
# prompting guide; the transcript itself is spoken verbatim).
STYLE_PRESETS = {
    "plain": ("ปกติ (ไม่ใส่สไตล์)", ""),
    "story": ("เล่านิทาน อบอุ่น", "warm, gentle bedtime storytelling, calm and unhurried"),
    "serious": ("จริงจังเตือนสติ", "serious and sincere, thoughtful, measured pace"),
    "sales": ("สดใสขายของ", "bright, upbeat and energetic, friendly and persuasive"),
}

# Clip rules. Docs: reference audio 10–30 s of clean natural speech; we
# recommend 20–30 s. Consent clip = one sentence (~5–10 s).
SOURCE_MIN_S = 10.0
SOURCE_RECOMMENDED_S = 15.0
SOURCE_MAX_S = 30.0
CONSENT_MIN_S = 2.0
CONSENT_MAX_S = 20.0

CHUNK_MAX_CHARS = 2500        # per TTS request (input limit 8,192 tokens; output 16,384 ≈ 10.9 min)
TEXT_MAX_CHARS = 20000        # whole job
CHUNK_GAP_S = 0.35            # silence inserted between chunks

MISSING_KEY_TH = (
    "ยังไม่มี GEMINI_API_KEY — ใส่บรรทัด GEMINI_API_KEY=... ใน "
    ".env (ขอคีย์ที่ Google AI Studio → Get API key) แล้วกดใหม่ได้เลย ไม่ต้องรีสตาร์ต"
)

_lock = threading.RLock()


class VoiceCloneError(Exception):
    """An error with a Thai, user-facing message."""

    def __init__(self, message: str, code: str = "error", detail: str = "", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail
        self.status = status

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.message, "code": self.code, "detail": self.detail}


# --------------------------------------------------------------------------- paths


@dataclass(frozen=True)
class Paths:
    root: Path = TOOL_DIR

    @property
    def voices_json(self) -> Path:
        return self.root / "voices.json"

    @property
    def samples(self) -> Path:
        return self.root / "samples"

    @property
    def pending(self) -> Path:
        return self.root / "samples" / "_pending"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def history_json(self) -> Path:
        return self.root / "out" / "history.json"


def _default_root() -> Path:
    """VOICE_CLONE_DATA overrides; mock mode keeps its fake voices/outputs in
    _mock/ so they never mix with the real voices.json."""
    env = os.environ.get("VOICE_CLONE_DATA", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if os.environ.get("VOICE_CLONE_MOCK", "").strip().lower() in ("1", "true", "yes", "on"):
        return TOOL_DIR / "_mock"
    return TOOL_DIR


DEFAULT_PATHS = Paths(root=_default_root())


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- env


def is_mock() -> bool:
    return os.environ.get("VOICE_CLONE_MOCK", "").strip().lower() in ("1", "true", "yes", "on")


def read_env_file(path: Path = ENV_FILE) -> dict:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].strip()
        values[k] = v.strip()
    return values


def get_api_key(env_file: Path = ENV_FILE) -> str:
    """Process env wins, then .env. Re-read on every call
    so pasting the key into .env works without restarting the server."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        key = read_env_file(env_file).get("GEMINI_API_KEY", "").strip()
    return key


def sdk_available() -> bool:
    try:
        import google.genai  # noqa: F401
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- audio


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            pass
    if not exe:
        raise VoiceCloneError("ไม่พบ FFmpeg — รันตัวติดตั้งตาม README หรือติดตั้ง FFmpeg ให้เรียกจาก PATH ได้", code="no_ffmpeg", status=500)
    return exe


CONTENT_TYPE_EXT = {
    "audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
    "video/mp4": ".mp4", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
}


def ext_for(content_type: str | None, filename: str | None = None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    if filename:
        suf = Path(filename).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{2,5}", suf or ""):
            return suf
    return ".bin"


def convert_to_wav(src: Path, dst: Path, max_seconds: float | None = None) -> None:
    """Any audio/video file → 24 kHz mono 16-bit PCM WAV."""
    cmd = [ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le"]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd.append(str(dst))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not dst.exists() or dst.stat().st_size <= 44:
        raise VoiceCloneError("อ่าน/แปลงไฟล์เสียงไม่ได้ — ลองอัดใหม่ หรือใช้ไฟล์ .wav/.mp3/.m4a",
                              code="bad_audio", detail=(p.stderr or "")[-400:])


def wav_to_mp3(src: Path, dst: Path) -> None:
    cmd = [ffmpeg_bin(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-codec:a", "libmp3lame", "-q:a", "2", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise VoiceCloneError("แปลงเป็น MP3 ไม่ได้", code="mp3_failed", detail=(p.stderr or "")[-400:], status=500)


def read_wav(path_or_bytes) -> tuple[bytes, int, int, int]:
    """Return (pcm_frames, sample_rate, channels, sample_width)."""
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb")
    with src, wave.open(src, "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels(), w.getsampwidth()


def write_wav(path: Path, pcm: bytes, rate: int = SAMPLE_RATE, channels: int = 1, width: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)


def audio_bytes_to_pcm(data: bytes) -> tuple[bytes, int]:
    """Gemini 3.8 unary responses are WAV (RIFF); streaming/l16 is headerless
    24 kHz mono s16le. Accept both → (pcm, rate)."""
    if data[:4] == b"RIFF":
        pcm, rate, ch, width = read_wav(data)
        if ch != 1 or width != 2:
            raise VoiceCloneError("รูปแบบเสียงที่ได้จาก API ไม่ใช่ 16-bit mono", code="bad_response", status=502)
        return pcm, rate
    if len(data) % 2:
        data = data[:-1]
    return data, SAMPLE_RATE


def _db(x: float) -> float:
    return 20 * math.log10(x) if x > 1e-9 else -120.0


def analyze_wav(path: Path) -> dict:
    pcm, rate, ch, width = read_wav(path)
    if width != 2:
        raise VoiceCloneError("ต้องเป็น WAV 16-bit", code="bad_audio")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    if ch > 1:
        samples = array.array("h", samples[::ch])
    n = len(samples)
    duration = n / float(rate) if rate else 0.0
    if n == 0:
        return {"duration": 0.0, "peak_dbfs": -120.0, "rms_dbfs": -120.0, "noise_floor_dbfs": -120.0,
                "speech_dbfs": -120.0, "snr_db": 0.0, "clipped_ratio": 0.0, "voiced_seconds": 0.0}
    frame = max(1, int(rate * 0.05))
    levels = []
    total_sq = 0.0
    peak = 0
    clipped = 0
    for i in range(0, n, frame):
        chunk = samples[i:i + frame]
        sq = 0
        for s in chunk:
            sq += s * s
            a = s if s >= 0 else -s
            if a > peak:
                peak = a
            if a >= 32700:
                clipped += 1
        total_sq += sq
        levels.append(_db(math.sqrt(sq / len(chunk)) / 32768.0))
    srt = sorted(levels)
    noise = srt[int(0.10 * (len(srt) - 1))]
    speech = srt[int(0.90 * (len(srt) - 1))]
    voiced_thr = max(-50.0, noise + 6.0)
    voiced = sum(1 for lv in levels if lv > voiced_thr) * frame / float(rate)
    return {
        "duration": round(duration, 2),
        "peak_dbfs": round(_db(peak / 32768.0), 1),
        "rms_dbfs": round(_db(math.sqrt(total_sq / n) / 32768.0), 1),
        "noise_floor_dbfs": round(noise, 1),
        "speech_dbfs": round(speech, 1),
        "snr_db": round(speech - noise, 1),
        "clipped_ratio": round(clipped / float(n), 5),
        "voiced_seconds": round(min(voiced, duration), 2),
    }


def check_clip(kind: str, m: dict, trimmed_from: float | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    d = m["duration"]
    if kind == "source":
        if d < SOURCE_MIN_S:
            errors.append(f"เสียงตัวอย่างสั้นเกินไป ({d:.1f} วิ) — ต้องอย่างน้อย {SOURCE_MIN_S:.0f} วิ แนะนำ 20–30 วิ")
        elif d < SOURCE_RECOMMENDED_S:
            warnings.append(f"ยาว {d:.1f} วิ ใช้ได้ แต่ถ้าอัด 20–30 วิ เสียงจะเหมือนกว่า")
        if trimmed_from:
            warnings.append(f"ไฟล์ยาว {trimmed_from:.1f} วิ เกิน 30 วิ — ตัดเหลือ 30 วิแรกให้แล้ว")
        if m["voiced_seconds"] < 6.0 and d >= SOURCE_MIN_S:
            errors.append("ช่วงที่มีเสียงพูดจริงน้อยเกินไป — พูดต่อเนื่องเป็นธรรมชาติ อย่าเว้นเงียบนาน")
    else:
        if d < CONSENT_MIN_S:
            errors.append(f"ประโยคยินยอมสั้นเกินไป ({d:.1f} วิ) — อ่านประโยคให้ครบทั้งประโยค")
        elif d > CONSENT_MAX_S:
            errors.append(f"ประโยคยินยอมยาวเกินไป ({d:.1f} วิ) — อ่านแค่ประโยคเดียว แล้วกดหยุด")
    if m["speech_dbfs"] < -45:
        errors.append("เสียงเบามาก/ไมค์ไม่ได้ยิน — เช็กว่าเลือกไมค์ถูกตัว แล้วพูดใกล้ไมค์ขึ้น")
    elif m["speech_dbfs"] < -32:
        warnings.append("เสียงค่อนข้างเบา — ขยับเข้าใกล้ไมค์อีกนิด")
    if m["clipped_ratio"] > 0.002:
        warnings.append("มีเสียงแตก (ดังเกิน) — ถอยห่างไมค์นิดนึง หรือพูดเบาลง")
    if m["snr_db"] < 6 and m["speech_dbfs"] >= -45:
        errors.append("เสียงรบกวนเยอะเกินไป แยกเสียงพูดไม่ออก — ย้ายไปห้องเงียบ ปิดพัดลม/แอร์ แล้วอัดใหม่")
    elif m["snr_db"] < 15 and m["speech_dbfs"] >= -45:
        warnings.append("มีเสียงรบกวนพื้นหลังพอสมควร — ถ้าได้ อัดในห้องที่เงียบกว่านี้จะดีกว่า")
    return errors, warnings


def prepare_clip(kind: str, raw: bytes, content_type: str | None = None, filename: str | None = None,
                 paths: Paths = DEFAULT_PATHS) -> dict:
    """Save an uploaded/recorded clip as samples/_pending/<kind>.wav (24 kHz mono
    16-bit), analyze it and return {ok, metrics, errors, warnings, ...}."""
    if kind not in ("source", "consent"):
        raise VoiceCloneError("kind ต้องเป็น source หรือ consent", code="bad_request")
    if not raw:
        raise VoiceCloneError("ไม่ได้รับไฟล์เสียง — ลองอัดใหม่", code="empty")
    paths.pending.mkdir(parents=True, exist_ok=True)
    wav = paths.pending / f"{kind}.wav"
    with tempfile.TemporaryDirectory(dir=paths.pending) as td:
        src = Path(td) / f"in{ext_for(content_type, filename)}"
        src.write_bytes(raw)
        tmp = Path(td) / "conv.wav"
        convert_to_wav(src, tmp)
        m = analyze_wav(tmp)
        trimmed_from = None
        if kind == "source" and m["duration"] > SOURCE_MAX_S:
            trimmed_from = m["duration"]
            pcm, rate, _, _ = read_wav(tmp)
            write_wav(tmp, pcm[: int(SOURCE_MAX_S * rate) * 2], rate)
            m = analyze_wav(tmp)
        errors, warnings = check_clip(kind, m, trimmed_from)
        with _lock:
            os.replace(tmp, wav)
    info = {
        "kind": kind, "ok": not errors, "metrics": m, "errors": errors, "warnings": warnings,
        "wav": str(wav.relative_to(paths.root)), "created_at": iso(now_utc()),
        "original": {"content_type": content_type or "", "filename": filename or "", "bytes": len(raw)},
    }
    (paths.pending / f"{kind}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def pending_status(paths: Paths = DEFAULT_PATHS) -> dict:
    out = {}
    for kind in ("source", "consent"):
        j = paths.pending / f"{kind}.json"
        w = paths.pending / f"{kind}.wav"
        if j.exists() and w.exists():
            try:
                out[kind] = json.loads(j.read_text(encoding="utf-8"))
            except ValueError:
                pass
    return out


# --------------------------------------------------------------------------- voices.json


def load_voices(paths: Paths = DEFAULT_PATHS) -> list[dict]:
    p = paths.voices_json
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
    except ValueError as e:
        raise VoiceCloneError(f"ไฟล์ {p.name} เสีย (JSON อ่านไม่ได้) — เปิดแก้หรือย้ายออกก่อน", code="voices_corrupt",
                              detail=str(e), status=500)
    if isinstance(data, dict):
        data = data.get("voices", [])
    return [v for v in data if isinstance(v, dict) and v.get("id")]


def save_voices(voices: list[dict], paths: Paths = DEFAULT_PATHS) -> None:
    p = paths.voices_json
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".voices-", suffix=".json", dir=p.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(voices, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def upsert_voice(record: dict, paths: Paths = DEFAULT_PATHS) -> dict:
    with _lock:
        voices = [v for v in load_voices(paths) if v.get("id") != record["id"]]
        voices.append(record)
        save_voices(voices, paths)
    return record


def find_voice(query: str, paths: Paths = DEFAULT_PATHS) -> dict | None:
    q = (query or "").strip()
    if not q:
        return None
    voices = load_voices(paths)
    for v in voices:
        if v.get("id") == q:
            return v
    # newest record wins when several share a display name (e.g. after recreate)
    for v in reversed(voices):
        if (v.get("display_name") or "").casefold() == q.casefold():
            return v
    return None


def touch_voice(voice_id: str, when: dt.datetime | None = None, paths: Paths = DEFAULT_PATHS) -> None:
    when = when or now_utc()
    with _lock:
        voices = load_voices(paths)
        changed = False
        for v in voices:
            if v.get("id") == voice_id:
                v["last_used_at"] = iso(when)
                if v.get("store", True):
                    v["expires"] = iso(when + dt.timedelta(days=STORED_TTL_DAYS))
                changed = True
        if changed:
            save_voices(voices, paths)


def is_expired(v: dict, when: dt.datetime | None = None) -> bool:
    exp = parse_iso(v.get("expires"))
    return bool(exp and exp <= (when or now_utc()))


def _slug(s: str) -> str:
    """Filesystem-safe name that keeps Thai intact (vowel/tone marks are
    category M*, which \\w does not match)."""
    import unicodedata
    out = []
    for ch in unicodedata.normalize("NFC", s.strip()):
        if ch.isalnum() or ch in "-_" or unicodedata.category(ch).startswith("M"):
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-")
    return (slug or "voice")[:40]


# --------------------------------------------------------------------------- text


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            rest = p
            while len(rest) > max_chars:
                window = rest[:max_chars]
                cut = max(window.rfind(c) for c in ("\n", ". ", "! ", "? ", "。", "…", " "))
                if cut < max_chars // 3:
                    cut = max_chars
                else:
                    cut += 1
                chunks.append(rest[:cut].strip())
                rest = rest[cut:].strip()
            if rest:
                cur = rest
        elif not cur:
            cur = p
        elif len(cur) + 2 + len(p) <= max_chars:
            cur = cur + "\n\n" + p
        else:
            chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


# --------------------------------------------------------------------------- API errors


def map_api_error(exc: Exception, api_key: str = "") -> VoiceCloneError:
    """Turn an SDK/HTTP exception into a Thai VoiceCloneError (never leaks the key)."""
    if isinstance(exc, VoiceCloneError):
        return exc
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None) or ""  # str (GenAiError) or parsed dict (compat BadRequestError etc.)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    raw = f"{exc} {body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)}"
    api_status = ""
    api_msg = ""
    try:
        j = body if isinstance(body, dict) else (json.loads(body) if body else {})
        err = j.get("error", j) if isinstance(j, dict) else {}
        err = err if isinstance(err, dict) else {}
        api_status = str(err.get("status", "") or "")
        api_msg = str(err.get("message", "") or "")
        status = status or err.get("code")
    except (ValueError, TypeError, AttributeError):
        pass
    detail = (api_msg or raw).strip()
    if api_key:
        detail = detail.replace(api_key, "***")
    detail = detail[:500]
    low = f"{raw} {api_msg} {api_status}".lower()
    name = type(exc).__name__.lower()

    if status is None and any(k in name for k in ("connect", "timeout", "network", "noresponse")):
        return VoiceCloneError("เชื่อมต่อ Google ไม่ได้ — เช็กอินเทอร์เน็ตแล้วลองใหม่", code="network", detail=detail, status=502)
    if "consent" in low:
        return VoiceCloneError(
            "ประโยคยินยอมไม่ผ่าน — ต้องอ่านประโยคตรงตัวทุกคำ (ภาษาไทยใช้ \"ฉัน\" ตามต้นฉบับ) ชัด ๆ ไม่มีคำอื่นแทรก "
            "และต้องเป็นคนเดียวกับเสียงตัวอย่าง อัดด้วยไมค์/ห้องเดียวกัน แล้วลองใหม่",
            code="consent_mismatch", detail=detail, status=400)
    if any(k in low for k in ("speaker verification", "same speaker", "speaker mismatch", "does not match", "verification failed")):
        return VoiceCloneError(
            "Google ตรวจว่าเสียงยินยอมกับเสียงตัวอย่างไม่ใช่คนเดียวกัน — อัดทั้งสองไฟล์ใหม่ด้วยไมค์เดียวกันในห้องเดียวกัน",
            code="consent_mismatch", detail=detail, status=400)
    if status == 429 or "resource_exhausted" in low or "quota" in low or "rate limit" in low:
        if "voice" in low and ("200" in low or "maximum" in low or "stored" in low):
            return VoiceCloneError("เสียงที่เก็บไว้ในโปรเจกต์เต็มโควตา (200 เสียง) — ลบเสียงเก่าใน AI Studio ก่อน",
                                   code="voice_quota", detail=detail, status=429)
        return VoiceCloneError(
            "โควตา/ลิมิตเต็ม — รอสัก 1 นาทีแล้วลองใหม่ ถ้ายังไม่ได้ให้เช็กโควตาที่ aistudio.google.com/rate-limit "
            "หรือเปิด billing (Paid tier)", code="quota", detail=detail, status=429)
    if status in (401, 403) or "api_key_invalid" in low or "api key not valid" in low or "permission_denied" in low:
        if "billing" in low or "paid" in low or "free tier" in low:
            return VoiceCloneError("ฟีเจอร์นี้ต้องเปิด billing (Paid tier) ในโปรเจกต์ของคีย์นี้ก่อน",
                                   code="billing", detail=detail, status=403)
        return VoiceCloneError("GEMINI_API_KEY ไม่ถูกต้องหรือไม่มีสิทธิ์ — สร้างคีย์ใหม่ใน AI Studio แล้ววางใน .env",
                               code="bad_key", detail=detail, status=401)
    if "billing" in low or "failed_precondition" in low:
        return VoiceCloneError("ใช้ฟีเจอร์นี้ไม่ได้กับโปรเจกต์/ภูมิภาคนี้ (อาจต้องเปิด billing) — ดูรายละเอียดด้านล่าง",
                               code="precondition", detail=detail, status=403)
    if status == 404 or "not_found" in low or "not found" in low:
        return VoiceCloneError("ไม่พบเสียงนี้ฝั่ง Google (อาจหมดอายุหรือถูกลบ) หรือชื่อโมเดลผิด — ถ้าเป็นเสียงที่หมดอายุ กด \"สร้างใหม่จากไฟล์เดิม\"",
                               code="voice_not_found", detail=detail, status=404)
    if status == 400 or "invalid_argument" in low:
        if any(k in low for k in ("audio", "duration", "too short", "too long", "sample")):
            return VoiceCloneError("ไฟล์เสียงไม่ผ่านเงื่อนไขของ Google (ความยาว/คุณภาพ) — อัดใหม่ 20–30 วิ ในห้องเงียบ",
                                   code="bad_sample", detail=detail, status=400)
        return VoiceCloneError("คำขอไม่ถูกต้อง — ดูรายละเอียดด้านล่าง", code="invalid", detail=detail, status=400)
    if isinstance(status, int) and status >= 500:
        return VoiceCloneError("ฝั่ง Google ขัดข้องชั่วคราว — ลองใหม่อีกครั้ง", code="server", detail=detail, status=502)
    return VoiceCloneError("เกิดข้อผิดพลาดจาก Gemini API — ดูรายละเอียดด้านล่าง", code="api_error", detail=detail, status=502)


# --------------------------------------------------------------------------- backends


def _get(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def extract_audio(interaction) -> bytes:
    """interaction.output_audio (SDK convenience) or steps[].content[] (REST)."""
    item = _get(interaction, "output_audio")
    if item is None:
        for step in reversed(_get(interaction, "steps") or []):
            if _get(step, "type") != "model_output":
                continue
            for c in reversed(_get(step, "content") or []):
                if _get(c, "type") == "audio":
                    item = c
                    break
            if item is not None:
                break
    data = _get(item, "data") if item is not None else None
    if not data:
        raise VoiceCloneError("API ไม่ได้ส่งเสียงกลับมา — ลองใหม่ หรือเปลี่ยนข้อความ", code="no_audio", status=502)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data) if bytes(data[:4]) == b"RIFF" else base64.b64decode(data)
    return base64.b64decode(data)


class GeminiBackend:
    name = "gemini"

    def __init__(self, api_key: str):
        try:
            from google import genai
        except ImportError as e:
            raise VoiceCloneError(
                "ยังไม่ได้ติดตั้ง google-genai — รัน python3 scripts/setup.py (Windows ใช้ py -3 scripts/setup.py)",
                code="no_sdk", detail=str(e), status=500)
        self._key = api_key
        self.client = genai.Client(api_key=api_key)

    def create_voice(self, display_name: str, source_wav: bytes, consent_wav: bytes, model: str) -> dict:
        voice = {
            "model": model,
            "type": "replicated",
            "display_name": display_name,
            "replicated": {
                "source_audio": {"mime_type": "audio/wav", "data": base64.b64encode(source_wav).decode("ascii")},
                "consent_audio": {"mime_type": "audio/wav", "data": base64.b64encode(consent_wav).decode("ascii")},
            },
        }
        try:
            v = self.client.voices.create(store=True, voice=voice)
        except Exception as e:  # noqa: BLE001 — mapped to Thai message
            raise map_api_error(e, self._key) from None
        vid = _get(v, "id") or _get(v, "key")
        if not vid:
            raise VoiceCloneError("API ไม่ได้ส่ง voice id กลับมา", code="bad_response", status=502)
        exp = _get(v, "expire_time")
        if isinstance(exp, dt.datetime):
            exp = iso(exp if exp.tzinfo else exp.replace(tzinfo=dt.timezone.utc))
        return {"id": vid, "expire_time": exp or None}

    def synthesize(self, voice_id: str, text: str, style: str, model: str) -> bytes:
        content = {"type": "text", "text": text}
        if style.strip():
            content["annotations"] = [{"type": "speech_metadata", "style": style.strip()}]
        try:
            it = self.client.interactions.create(
                model=model,
                input=[{"type": "user_input", "content": [content]}],
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": voice_id}]},
            )
        except Exception as e:  # noqa: BLE001
            raise map_api_error(e, self._key) from None
        return extract_audio(it)

    def list_remote(self) -> list[dict]:
        try:
            r = self.client.voices.list(type_=["replicated"])
        except Exception as e:  # noqa: BLE001
            raise map_api_error(e, self._key) from None
        out = []
        for v in _get(r, "voices") or []:
            exp = _get(v, "expire_time")
            out.append({"id": _get(v, "id"), "display_name": _get(v, "display_name"),
                        "type": str(_get(v, "type") or ""), "model": _get(v, "model"),
                        "expire_time": iso(exp) if isinstance(exp, dt.datetime) else exp})
        return out


class MockBackend:
    """VOICE_CLONE_MOCK=1 — no network. Voice ids look like voice_mock_xxx; speech
    is macOS `say` (Thai voice Kanya when available) or a tone."""

    name = "mock"

    def create_voice(self, display_name: str, source_wav: bytes, consent_wav: bytes, model: str) -> dict:
        if "mock-consent-fail" in display_name.lower():
            raise map_api_error(_FakeApiError(400, "INVALID_ARGUMENT", "Consent audio does not match the required consent statement."))
        if "mock-quota" in display_name.lower():
            raise map_api_error(_FakeApiError(429, "RESOURCE_EXHAUSTED", "Quota exceeded."))
        return {"id": "voice_mock_" + uuid.uuid4().hex[:12],
                "expire_time": iso(now_utc() + dt.timedelta(days=STORED_TTL_DAYS))}

    def synthesize(self, voice_id: str, text: str, style: str, model: str) -> bytes:
        if "[[quota]]" in text:
            raise map_api_error(_FakeApiError(429, "RESOURCE_EXHAUSTED", "Quota exceeded for metric."))
        spoken = re.sub(r"<[^>]{1,30}>", " ", text).strip() or "ทดสอบ"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "mock.wav"
            say = shutil.which("say")
            if say and not os.environ.get("VOICE_CLONE_MOCK_TONE"):
                aiff = Path(td) / "mock.aiff"
                args = [say, "-o", str(aiff)]
                if re.search(r"[฀-๿]", spoken):
                    args += ["-v", "Kanya"]
                p = subprocess.run(args + [spoken[:4000]], capture_output=True)
                if p.returncode == 0 and aiff.exists():
                    convert_to_wav(aiff, out)
                    return out.read_bytes()
            seconds = max(1.0, min(60.0, len(spoken) / 14.0))
            n = int(SAMPLE_RATE * seconds)
            a = array.array("h", (int(6000 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)) for i in range(n)))
            if sys.byteorder == "big":
                a.byteswap()
            write_wav(out, a.tobytes())
            return out.read_bytes()

    def list_remote(self) -> list[dict]:
        return []


class _FakeApiError(Exception):
    def __init__(self, code: int, status: str, message: str):
        super().__init__(message)
        self.status_code = code
        self.body = json.dumps({"error": {"code": code, "status": status, "message": message}})


def get_backend():
    if is_mock():
        return MockBackend()
    key = get_api_key()
    if not key:
        raise VoiceCloneError(MISSING_KEY_TH, code="missing_key", status=400)
    return GeminiBackend(key)


# --------------------------------------------------------------------------- high level


def create_voice(display_name: str, consent_lang: str = "th-TH", model: str = DEFAULT_MODEL,
                 source_wav: Path | None = None, consent_wav: Path | None = None,
                 backend=None, paths: Paths = DEFAULT_PATHS) -> dict:
    """Create a stored (store=True) replicated voice from the pending clips (or
    the given WAVs), keep both WAVs under samples/<name>-<time>/ and record it
    in voices.json."""
    display_name = (display_name or "").strip()[:60]
    if not display_name:
        raise VoiceCloneError("ตั้งชื่อเสียงก่อน เช่น \"เสียงของฉัน\"", code="no_name")
    if model not in MODELS:
        raise VoiceCloneError(f"ไม่รู้จักโมเดล {model}", code="bad_model")
    from_pending = source_wav is None and consent_wav is None
    if from_pending:
        pend = pending_status(paths)
        for kind, label in (("source", "เสียงตัวอย่าง (ขั้นที่ 1)"), ("consent", "ประโยคยินยอม (ขั้นที่ 2)")):
            if kind not in pend:
                raise VoiceCloneError(f"ยังไม่มี{label} — อัดก่อนแล้วค่อยสร้างเสียง", code=f"missing_{kind}")
            if not pend[kind].get("ok"):
                raise VoiceCloneError(f"{label} ยังไม่ผ่าน: " + " / ".join(pend[kind].get("errors", [])),
                                      code=f"bad_{kind}")
        source_wav = paths.pending / "source.wav"
        consent_wav = paths.pending / "consent.wav"
    if not (source_wav and consent_wav and Path(source_wav).exists() and Path(consent_wav).exists()):
        raise VoiceCloneError("ไม่พบไฟล์เสียงตัวอย่าง/ประโยคยินยอม", code="missing_files")
    backend = backend or get_backend()
    res = backend.create_voice(display_name, Path(source_wav).read_bytes(), Path(consent_wav).read_bytes(), model)
    created = now_utc()
    folder = paths.samples / f"{_slug(display_name)}-{created.strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)
    keep_src, keep_con = folder / "source.wav", folder / "consent.wav"
    if from_pending:
        os.replace(source_wav, keep_src)
        os.replace(consent_wav, keep_con)
        for kind in ("source", "consent"):
            (paths.pending / f"{kind}.json").unlink(missing_ok=True)
    else:
        if Path(source_wav).resolve() != keep_src.resolve():
            shutil.copy2(source_wav, keep_src)
        if Path(consent_wav).resolve() != keep_con.resolve():
            shutil.copy2(consent_wav, keep_con)
    record = {
        "id": res["id"],
        "display_name": display_name,
        "created_at": iso(created),
        "expires": res.get("expire_time") or iso(created + dt.timedelta(days=STORED_TTL_DAYS)),
        "last_used_at": None,
        "model": model,
        "type": "replicated",
        "store": True,
        "consent_lang": consent_lang,
        "source_wav": str(keep_src.relative_to(paths.root)),
        "consent_wav": str(keep_con.relative_to(paths.root)),
        "mock": backend.name == "mock",
    }
    upsert_voice(record, paths)
    return record


def recreate_voice(voice_id: str, backend=None, paths: Paths = DEFAULT_PATHS) -> dict:
    """Re-run voices.create from the WAVs kept for an (expired) voice."""
    old = find_voice(voice_id, paths)
    if not old:
        raise VoiceCloneError("ไม่พบเสียงนี้ใน voices.json", code="voice_not_found", status=404)
    src = paths.root / old.get("source_wav", "")
    con = paths.root / old.get("consent_wav", "")
    if not (src.is_file() and con.is_file()):
        raise VoiceCloneError("ไม่พบไฟล์เสียงเดิมที่เก็บไว้ — ต้องอัดใหม่", code="missing_files", status=404)
    rec = create_voice(old["display_name"], old.get("consent_lang", "th-TH"), old.get("model", DEFAULT_MODEL),
                       source_wav=src, consent_wav=con, backend=backend, paths=paths)
    with _lock:
        voices = load_voices(paths)
        for v in voices:
            if v.get("id") == old["id"]:
                v["replaced_by"] = rec["id"]
        save_voices(voices, paths)
    return rec


def resolve_voice(query: str, paths: Paths = DEFAULT_PATHS) -> tuple[str, dict | None]:
    """voices.json id/name → (voice_id, record). Anything else is passed through
    as a raw id (voice_..., voicekey_...) or a prebuilt voice name (e.g. Kore)."""
    q = (query or "").strip()
    if q.startswith("gemini:"):
        q = q[len("gemini:"):]
    if not q:
        raise VoiceCloneError("ยังไม่ได้เลือกเสียง", code="no_voice")
    rec = find_voice(q, paths)
    if rec and rec.get("replaced_by"):
        newer = find_voice(rec["replaced_by"], paths)
        if newer:
            rec = newer
    return (rec["id"] if rec else q), rec


def speak(voice: str, text: str, style: str = "", model: str = DEFAULT_MODEL, out_path: Path | None = None,
          backend=None, paths: Paths = DEFAULT_PATHS, record_history: bool = False, progress=None) -> dict:
    """Synthesize `text` with `voice` → WAV (+ MP3 if out_path ends with .mp3 or
    when writing into out/). Long text is split into ≤2,500-char chunks."""
    text = (text or "").strip()
    if not text:
        raise VoiceCloneError("ยังไม่ได้ใส่ข้อความ", code="no_text")
    if len(text) > TEXT_MAX_CHARS:
        raise VoiceCloneError(f"ข้อความยาวเกิน {TEXT_MAX_CHARS:,} ตัวอักษร — แบ่งเป็นหลายตอน", code="too_long")
    if model not in MODELS:
        raise VoiceCloneError(f"ไม่รู้จักโมเดล {model}", code="bad_model")
    voice_id, rec = resolve_voice(voice, paths)
    if rec and rec.get("mock") and not is_mock():
        raise VoiceCloneError("เสียงนี้สร้างในโหมดจำลอง (mock) ใช้กับ API จริงไม่ได้ — สร้างเสียงจริงใหม่", code="mock_voice")
    backend = backend or get_backend()
    chunks = chunk_text(text)
    pcm_parts: list[bytes] = []
    rate = SAMPLE_RATE
    for i, ch in enumerate(chunks):
        if progress:
            progress(i, len(chunks))
        pcm, r = audio_bytes_to_pcm(backend.synthesize(voice_id, ch, style or "", model))
        if pcm_parts and r != rate:
            raise VoiceCloneError("sample rate ไม่ตรงกันระหว่างท่อน", code="bad_response", status=502)
        rate = r
        if pcm_parts:
            pcm_parts.append(b"\x00\x00" * int(CHUNK_GAP_S * rate))
        pcm_parts.append(pcm)
    pcm_all = b"".join(pcm_parts)
    created = now_utc()
    if out_path is None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = _slug((rec or {}).get("display_name") or voice_id)
        base = paths.out / f"{stamp}-{name}-{uuid.uuid4().hex[:4]}"
        wav_path, mp3_path = base.with_suffix(".wav"), base.with_suffix(".mp3")
    else:
        out_path = Path(out_path)
        if out_path.suffix.lower() == ".mp3":
            wav_path, mp3_path = out_path.with_suffix(".wav"), out_path
        else:
            wav_path, mp3_path = out_path, None
    write_wav(wav_path, pcm_all, rate)
    if mp3_path:
        wav_to_mp3(wav_path, mp3_path)
        if out_path is not None:  # CLI asked for .mp3 only
            wav_path.unlink(missing_ok=True)
            wav_path = None
    if rec:
        touch_voice(rec["id"], created, paths)
    duration = round(len(pcm_all) / 2 / float(rate), 2)
    item = {
        "id": uuid.uuid4().hex[:10],
        "created_at": iso(created),
        "voice_id": voice_id,
        "voice_name": (rec or {}).get("display_name") or voice_id,
        "style": style or "",
        "model": model,
        "chars": len(text),
        "chunks": len(chunks),
        "text_preview": text[:160],
        "duration": duration,
        "wav": str(wav_path) if wav_path else None,
        "mp3": str(mp3_path) if mp3_path else None,
        "mock": backend.name == "mock",
    }
    for k in ("wav", "mp3"):
        if item[k]:
            try:
                item[k] = str(Path(item[k]).resolve().relative_to(paths.root.resolve()))
            except ValueError:
                pass
    if record_history:
        add_history(item, paths)
    return item


def load_history(paths: Paths = DEFAULT_PATHS) -> list[dict]:
    try:
        data = json.loads(paths.history_json.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def add_history(item: dict, paths: Paths = DEFAULT_PATHS, keep: int = 200) -> None:
    with _lock:
        hist = [item] + load_history(paths)
        paths.out.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".json", dir=paths.out)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(hist[:keep], f, ensure_ascii=False, indent=2)
        os.replace(tmp, paths.history_json)
