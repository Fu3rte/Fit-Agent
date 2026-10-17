import argparse
import json
import sqlite3
import time
from pathlib import Path


def wait_for_release(release_file: Path | None, hold_seconds: float) -> None:
    deadline = time.monotonic() + hold_seconds
    while time.monotonic() < deadline:
        if release_file is not None and release_file.exists():
            return
        time.sleep(0.02)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=("exclusive", "shared"), required=True)
    parser.add_argument("--hold-seconds", type=float, default=60.0)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--release-file", default=None)
    args = parser.parse_args()

    ready = Path(args.ready_file)
    release = Path(args.release_file) if args.release_file else None
    ready.parent.mkdir(parents=True, exist_ok=True)
    if release is not None and release.exists():
        release.unlink()

    connection = sqlite3.connect(args.db, timeout=0.1, isolation_level=None)
    acquired_at = None
    try:
        if args.mode == "exclusive":
            deadline = time.monotonic() + 20.0
            while True:
                try:
                    connection.execute("BEGIN EXCLUSIVE")
                    break
                except sqlite3.OperationalError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        else:
            connection.execute("BEGIN")
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        acquired_at = time.time()
        ready.write_text(json.dumps({"pid": __import__("os").getpid(), "mode": args.mode}), encoding="utf-8")
        wait_for_release(release, args.hold_seconds)
    finally:
        try:
            connection.execute("COMMIT" if args.mode == "shared" else "ROLLBACK")
        except sqlite3.Error:
            pass
        connection.close()
    print(json.dumps({"mode": args.mode, "acquired_at": acquired_at, "released_at": time.time()}))


if __name__ == "__main__":
    main()
