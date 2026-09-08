"""Native launcher with single-instance handling and ordinary Chrome startup."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

from app.server import DEFAULT_PORT, data_dir, initialize_data_dirs, open_reader, run


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as response:
            return response.status == 200 and json.load(response).get("ok") is True
    except Exception:
        return False


def _open_existing(state_file: Path, no_browser: bool) -> bool:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        port = int(state["port"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if not _healthy(port):
        return False
    if not no_browser:
        open_reader(f"http://127.0.0.1:{port}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    root = initialize_data_dirs()
    state_file = root / "instance.json"
    lock_path = root / "instance.lock"
    lock = lock_path.open("a+")
    try:
        if os.name != "nt":
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                for _ in range(50):
                    if _open_existing(state_file, arguments.no_browser):
                        return
                    time.sleep(0.1)
                raise RuntimeError("Bilingual Manga Reader and Mokuro Converter is starting but did not become responsive")
        elif _open_existing(state_file, arguments.no_browser):
            return
        # A stale state file is harmless once this process owns the lock.
        state_file.unlink(missing_ok=True)
        run(arguments.port, not arguments.no_browser, state_file)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
