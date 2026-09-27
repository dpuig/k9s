"""HTTP + SSE over a Unix socket (ADR-0002).

POST /v1/<task> with a JSON Request body streams `event: <kind>` / `data: <json>`
frames and ends with `event: done`. A client disconnect cancels the handler, which
closes the SDK agent and stops generation. The daemon exits after idle_minutes
with no requests in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import socket
import time
from pathlib import Path

from aiohttp import web

from k9sai import tasks
from k9sai.tasks import Deps, Request

log = logging.getLogger("k9sai")
VERSION = "0.1.0"


def default_socket() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "k9sai" if runtime else Path.home() / ".local" / "state" / "k9sai"
    return base / "daemon.sock"


class Activity:
    def __init__(self) -> None:
        self.inflight = 0
        self.last = time.monotonic()

    @contextlib.contextmanager
    def track(self):
        self.inflight += 1
        try:
            yield
        finally:
            self.inflight -= 1
            self.last = time.monotonic()


def _frame(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def make_app(deps: Deps, activity: Activity) -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": VERSION})

    async def run(req: web.Request) -> web.StreamResponse:
        task = req.match_info["task"]
        handler = tasks.TASKS.get(task)
        if handler is None:
            return web.json_response({"error": f"unknown task {task}"}, status=404)
        try:
            body = await req.json()
            fields = {f.name for f in dataclasses.fields(Request)} - {"task"}
            r = Request(task=task, **{k: v for k, v in body.items() if k in fields})
        except (ValueError, TypeError) as e:
            return web.json_response({"error": f"bad request: {e}"}, status=400)

        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(req)
        with activity.track():
            try:
                async for event, data in handler(deps, r):
                    await resp.write(_frame(event, data))
            except (ConnectionResetError, asyncio.CancelledError):
                log.info("client went away; %s cancelled", task)
                raise
            except Exception as e:  # surfaced to the pager, never swallowed
                log.exception("%s failed", task)
                await resp.write(_frame("error", {"message": f"{type(e).__name__}: {e}"}))
            await resp.write(_frame("done", {}))
        return resp

    app = web.Application()
    app.router.add_get("/healthz", health)
    app.router.add_post("/v1/{task}", run)
    return app


MAX_SOCKET_PATH = 103  # sun_path is 104 bytes on macOS, 108 on Linux


def _prepare_socket(path: Path) -> None:
    if len(str(path).encode()) > MAX_SOCKET_PATH:
        raise SystemExit(f"socket path too long for AF_UNIX ({len(str(path))} bytes): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()  # stale socket from a dead daemon
        else:
            raise SystemExit(f"daemon already running on {path}")
        finally:
            probe.close()


async def serve(deps: Deps, path: Path) -> None:
    _prepare_socket(path)
    activity = Activity()
    runner = web.AppRunner(make_app(deps, activity), handler_cancellation=True)
    await runner.setup()
    old = os.umask(0o177)  # socket is created 0600
    try:
        site = web.UnixSite(runner, str(path))
        await site.start()
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    log.info("k9sai daemon %s listening on %s", VERSION, path)
    idle = deps.cfg.idle_minutes * 60
    try:
        while True:
            await asyncio.sleep(min(30, idle))
            if activity.inflight == 0 and time.monotonic() - activity.last >= idle:
                log.info("idle for %d minutes; exiting", deps.cfg.idle_minutes)
                break
    finally:
        await runner.cleanup()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
