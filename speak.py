#!/usr/bin/env python3
"""speak.py — Voice Clone CLI: synthesize speech with a cloned (or prebuilt) Gemini voice.

  python3 speak.py --voice "เสียงของฉัน" --style "warm bedtime storytelling" \
      --text "กาลครั้งหนึ่ง..." --out story.mp3
  python3 speak.py --voice voice_abc123 --text-file story.txt --out story.wav
  python3 speak.py --list            # voices in voices.json
  python3 speak.py --list --remote   # replicated voices stored in the Google project

--voice accepts a voices.json id or display name, "gemini:<id>", a raw
voice_.../voicekey_... id, or a prebuilt voice name (Kore, Puck, ...).
--style accepts a preset key (plain/story/serious/sales) or free text.
VOICE_CLONE_MOCK=1 fakes the API (no key needed).

Runs on system python3: if google-genai is missing it re-execs itself with
the project .venv (created by scripts/setup.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voiceclone as vc  # noqa: E402


def _maybe_reexec_in_venv() -> None:
    if vc.is_mock() or vc.sdk_available():
        return
    venv = vc.VENV_PYTHON
    # compare prefixes, not executables: a venv's python3 is a symlink to the base interpreter
    in_venv = Path(sys.prefix).resolve() == venv.parent.parent.resolve()
    if venv.exists() and not in_venv and not os.environ.get("_VC_REEXEC"):
        os.environ["_VC_REEXEC"] = "1"
        os.execv(str(venv), [str(venv), str(Path(__file__).resolve()), *sys.argv[1:]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Speak with a Gemini cloned voice (Voice Clone).")
    ap.add_argument("--voice", help="voices.json id/name, gemini:<id>, voice_..., or prebuilt name")
    ap.add_argument("--style", default="", help="preset (plain/story/serious/sales) or free-text direction")
    ap.add_argument("--text", help="text to speak (spoken verbatim; inline tags like <short pause> ok)")
    ap.add_argument("--text-file", help="read text from a UTF-8 file ('-' = stdin)")
    ap.add_argument("--out", help="output .wav or .mp3 (default: out/<time>-<voice>.wav+.mp3)")
    ap.add_argument("--model", default=vc.DEFAULT_MODEL, choices=sorted(vc.MODELS))
    ap.add_argument("--list", action="store_true", help="list voices and exit")
    ap.add_argument("--remote", action="store_true", help="with --list: ask the Google project instead of voices.json")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    if not (a.list and not a.remote):
        _maybe_reexec_in_venv()  # before reading stdin, so a re-exec loses nothing
    try:
        if a.list:
            if a.remote:
                voices = vc.get_backend().list_remote()
            else:
                voices = vc.load_voices()
                for v in voices:
                    v["expired"] = vc.is_expired(v)
            if a.json:
                print(json.dumps(voices, ensure_ascii=False, indent=2))
            elif not voices:
                print("(ยังไม่มีเสียง)")
            else:
                for v in voices:
                    flag = " [หมดอายุ]" if v.get("expired") else ""
                    flag += " [mock]" if v.get("mock") else ""
                    flag += f" [แทนที่ด้วย {v['replaced_by']}]" if v.get("replaced_by") else ""
                    print(f"{v.get('id')}\t{v.get('display_name')}\tหมดอายุ {v.get('expires') or v.get('expire_time')}{flag}")
            return 0

        if not a.voice:
            ap.error("--voice is required (or use --list)")
        if a.text_file:
            text = sys.stdin.read() if a.text_file == "-" else Path(a.text_file).read_text(encoding="utf-8")
        else:
            text = a.text or ""
        style = vc.STYLE_PRESETS[a.style][1] if a.style in vc.STYLE_PRESETS else a.style

        def progress(i, n):
            if n > 1 and not a.json:
                print(f"  ท่อน {i + 1}/{n} ...", file=sys.stderr)

        item = vc.speak(a.voice, text, style=style, model=a.model,
                        out_path=Path(a.out).expanduser().resolve() if a.out else None,
                        record_history=not a.out, progress=progress)
        for k in ("mp3", "wav"):  # absolute paths for callers (factory / night runner)
            if item.get(k) and not Path(item[k]).is_absolute():
                item[k] = str(vc.DEFAULT_PATHS.root / item[k])
        if a.json:
            print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            for k in ("mp3", "wav"):
                if item.get(k):
                    p = Path(item[k])
                    print(str(p if p.is_absolute() else (vc.DEFAULT_PATHS.root / p)))
            print(f"ยาว {item['duration']:.1f} วิ · {item['chunks']} ท่อน · เสียง {item['voice_name']}"
                  + (" · MOCK" if item["mock"] else ""), file=sys.stderr)
        return 0
    except vc.VoiceCloneError as e:
        if a.json:
            print(json.dumps(e.to_dict(), ensure_ascii=False))
        else:
            print(f"ผิดพลาด: {e.message}", file=sys.stderr)
            if e.detail:
                print(f"  รายละเอียด: {e.detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
