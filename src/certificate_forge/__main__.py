"""Convenience launcher: one command starts the API and a separate worker process."""

import argparse
import logging
import os
import subprocess
import sys

import uvicorn

from certificate_forge.config import Settings
from certificate_forge.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Certificate Forge API and worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database = Database(Settings.from_env())
    database.initialize()
    database.close()
    worker = subprocess.Popen(
        [sys.executable, "-m", "certificate_forge.worker"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        uvicorn.run(
            "certificate_forge.app:create_app", factory=True, host=args.host, port=args.port
        )
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


if __name__ == "__main__":
    main()
