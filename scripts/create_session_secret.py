#!/usr/bin/env python3
"""Create a private signing key for anonymous upload sessions; never print it."""
from __future__ import annotations

import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    destination = ROOT / ".env.cloud-session"
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(".env.cloud-session already exists; nothing was changed.")
        print("Keep it, or move it to private storage before generating a replacement.")
        return 2
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(f"SESSION_SECRET={secrets.token_hex(32)}\n")
    print("Saved .env.cloud-session (ignored by Git and deployment).")
    print("Copy SESSION_SECRET privately into this Vercel project's Environment Variables.")
    print("This signs upload sessions; the studio has no login password. Do not share this file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
