#!/usr/bin/env python3
"""Create this project's isolated environment without reading another app's keys."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up Doctor Top CPD locally")
    parser.add_argument("--dev", action="store_true", help="include test dependencies")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print("Python 3.11+ is required; Python 3.13 is recommended.", file=sys.stderr)
        return 2
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(env)
    requirement = ROOT / ("requirements-dev.txt" if args.dev else "requirements.txt")
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(requirement)], check=True)
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
        if os.name != "nt":
            (ROOT / ".env").chmod(0o600)
    subprocess.run([str(python), "-c", "import voiceclone; print('FFmpeg:', voiceclone.ffmpeg_bin())"], cwd=ROOT, check=True)
    print("Setup complete. Put your own GEMINI_API_KEY in .env, then run: python run.py --open")
    print("To explore without API calls: python run.py --mock --open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
