from __future__ import annotations

import argparse
import os

import uvicorn

from zeaz_web.dashboard import create_app

app = create_app()


def run() -> None:
    parser = argparse.ArgumentParser(prog="zeaz-web")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0a1")
    parser.parse_args()
    uvicorn.run(
        "zeaz_web.main:app",
        host=os.getenv("ZEAZ_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("ZEAZ_WEB_PORT", "8070")),
        workers=1,
    )
