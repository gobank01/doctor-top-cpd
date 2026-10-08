#!/usr/bin/env python3
"""Check the public file set, without printing matching secret values.

Inside a standalone Git checkout scans tracked + non-ignored new files.
Before Git initialization scans source files excluding local private/runtime data.
This supplements manual review; it is not an exhaustive credential detector.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_PARTS = {".git", ".venv", ".vercel", ".migration", "samples", "out", "_mock", "logs", "artifacts", "dist", "__pycache__"}
PRIVATE_NAMES = {"ACCESS.md", "voices.json", ".DS_Store"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".aiff", ".ogg", ".flac", ".webm", ".mp4"}
PATTERNS = {
    "Google API key": re.compile(r"AI" + r"za[0-9A-Za-z_-]{35}"),
    "OpenAI/OpenRouter key": re.compile(r"sk-" + r"(?:or-v1-)?[A-Za-z0-9_-]{32,}"),
    "Vercel Blob token": re.compile(r"vercel_blob_" + r"rw_[A-Za-z0-9_-]{20,}"),
    "GitHub credential": re.compile(r"(?:gh" + r"[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "private key": re.compile(r"-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal absolute path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "Vercel project identity": re.compile(r"\b(?:prj|team)_" + r"[A-Za-z0-9]{16,}\b"),
}


def forbidden(path: Path) -> bool:
    return bool(set(path.parts) & PRIVATE_PARTS or path.name in PRIVATE_NAMES
                or (path.name.startswith(".env") and path.name != ".env.example")
                or path.suffix.lower() in AUDIO_EXTENSIONS)


def public_files() -> list[Path]:
    # Never accidentally use an enclosing workspace's Git repository.
    if (ROOT / ".git").exists():
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], check=True, capture_output=True)
        return sorted({Path(p) for p in result.stdout.decode().split("\0") if p})
    return sorted(p.relative_to(ROOT) for p in ROOT.rglob("*")
                  if p.is_file() and not forbidden(p.relative_to(ROOT)))


def main() -> int:
    findings: list[str] = []
    files = public_files()
    for relative in files:
        if forbidden(relative):
            findings.append(f"{relative}: private/runtime file in public set")
            continue
        path = ROOT / relative
        if path.is_symlink():
            findings.append(f"{relative}: symlink is not allowed in release")
            continue
        if not path.is_file():
            continue  # a staged deletion will be removed at the next commit
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(f"{relative}: unexpected binary file; review before release")
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{relative}: possible {label} (value hidden)")
        if relative.name == ".env.example":
            for line in content.splitlines():
                if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if value.strip():
                        findings.append(f".env.example: {key.strip()} must be blank")
    if findings:
        print("RELEASE CHECK FAILED")
        print("\n".join(findings))
        return 1
    print(f"PASS: {len(files)} public source files checked; no configured credential/path patterns found.")
    print("Review git diff/history as well. This check never certifies all secrets are absent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
