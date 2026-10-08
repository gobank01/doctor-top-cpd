#!/usr/bin/env python3
"""Cross-platform local launcher. Use scripts/setup.py once before running."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Voice Clone on this computer")
    parser.add_argument("--mock", action="store_true", help="no real API calls")
    parser.add_argument("--open", action="store_true", help="open your browser")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Run setup first: python scripts/setup.py", file=sys.stderr)
        return 2
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
    except OSError:
        print(f"Port {args.port} is busy. Try: python run.py --port 8788", file=sys.stderr)
        return 2
    child_env = os.environ.copy()
    # Explicit mode keeps a previous shell's mock setting from changing this launch.
    child_env["VOICE_CLONE_MOCK"] = "1" if args.mock else "0"
    url = f"http://localhost:{args.port}"
    print(f"Voice Clone: {url}", flush=True)
    if args.open:
        timer = threading.Timer(1.2, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    try:
        return subprocess.call([str(python), str(ROOT / "server.py"), "--port", str(args.port)], cwd=ROOT, env=child_env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
