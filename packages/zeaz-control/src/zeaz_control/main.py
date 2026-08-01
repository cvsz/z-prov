"""Minimal separately deployable control-plane process."""

from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from zeaz_control.models import ControlStore


def _state_path() -> Path:
    return Path(os.getenv("ZEAZ_CONTROL_STATE", "/var/lib/zeaz-control/control.sqlite3"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = _state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    app.state.control_store = ControlStore(path)
    yield


app = FastAPI(
    title="ZeaZ Control",
    version="0.1.0a1",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


def run() -> None:
    parser = argparse.ArgumentParser(prog="zeaz-control")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0a1")
    parser.parse_args()
    uvicorn.run(
        "zeaz_control.main:app",
        host=os.getenv("ZEAZ_CONTROL_HOST", "127.0.0.1"),
        port=int(os.getenv("ZEAZ_CONTROL_PORT", "8090")),
        workers=1,
    )
