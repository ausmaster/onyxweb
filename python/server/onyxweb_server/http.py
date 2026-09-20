"""The HTTP front-end: one route per browser operation, whole snapshots on the wire.

A program calling this wants the page, not a digest of it, so the answer to ``POST /fetch``
is `RenderResult.snapshot()` as JSON, zstd-compressed when the caller accepts it, and
``RenderResult.load`` rebuilds the page from it. The server holds nothing between requests.
Every request goes through `ServerCore.fetch`, so the URL guard and the ceilings apply as they
do for MCP, and the fields that would run caller JavaScript are refused by name.

A route reads as its happy path: what the core refuses or the browser fails at is turned into
an answer by the handlers registered in `build_app`, so every route maps a cause the same way.

There is no authentication, and ``onyxweb-server http`` binds loopback by default: put a
reverse proxy in front before exposing it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

try:
    import zstandard
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response
except ImportError as ie:
    raise ImportError(
        "onyxweb_server.http needs fastapi and zstandard; "
        'install them with pip install "onyxweb-server[http]".'
    ) from ie

import onyxweb

from onyxweb_server.core import FetchOptions, Refused, ServerCore, TooLarge

REFUSED_FIELDS: Final = ("scripts", "post_load_scripts", "actions")
ZSTD_LEVEL: Final = 3  # 9.2x on a 7 MB page in 11 ms; higher levels cost 100x the time for 20% more
# The `.kind` of a failed fetch -> the HTTP status. A kind not listed here is the server's fault.
STATUS: Final = {
    "invalid_url": 400,
    "invalid_config": 400,
    "post_load_script": 400,
    "chrome_not_found": 503,
    "launch_failed": 503,
    "chrome_exited": 503,
    "queue_timeout": 503,
    "cdp": 502,
    "io": 502,
    "navigation_timeout": 504,
    "timeout": 504,
    "internal": 500,
}


class FetchRequest(BaseModel):
    """The body of ``POST /fetch``: every field the server accepts."""

    model_config = ConfigDict(extra="forbid")

    url: str
    engine: str = "shell"
    wait_ms: int = 0


def _error(request: Request, status: int, kind: str, message: str) -> JSONResponse:
    """One error body, naming the URL the route was working on when it has one."""
    url = getattr(request.state, "url", None)
    return JSONResponse(
        {"error": {"kind": kind, "message": message, "url": url}}, status_code=status
    )


def _encoded(page: onyxweb.RenderResult, accept_encoding: str) -> tuple[bytes, dict[str, str]]:
    """The snapshot as it goes on the wire, with its headers: zstd when the caller offers it.

    Reads the ``Accept-Encoding`` value for a zstd offer with a weight above zero. Runs in a
    thread, since compressing a large page holds the loop otherwise.
    """
    compress = False
    for part in accept_encoding.split(","):
        coding, *params = (piece.strip() for piece in part.split(";"))
        if coding.lower() != "zstd":
            continue
        try:
            weight = next((float(p[2:]) for p in params if p.lower().startswith("q=")), 1.0)
        except ValueError:
            break
        compress = weight > 0
        break
    body = json.dumps(page.snapshot(), ensure_ascii=False).encode()
    headers = {"Vary": "Accept-Encoding"}
    if compress:
        return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(body), headers | {
            "Content-Encoding": "zstd"
        }
    return body, headers


def build_app(core: ServerCore | None = None) -> FastAPI:
    """Build the HTTP app over `core`, or over a default core when none is given."""
    core = core or ServerCore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await core.aclose()

    app = FastAPI(title="onyxweb-server", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(TooLarge)
    async def too_large(request: Request, exc: TooLarge) -> JSONResponse:
        """A page or image over a limit is the caller's to make smaller."""
        return _error(request, 413, exc.code, str(exc))

    @app.exception_handler(Refused)
    async def refused(request: Request, exc: Refused) -> JSONResponse:
        """Anything else the core will not serve is a bad request."""
        return _error(request, 400, "invalid_request", str(exc))

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, exc: ValueError) -> JSONResponse:
        """A route's own ValueError names the input that was wrong; nothing here is a bug."""
        return _error(request, 400, "invalid_request", str(exc))

    async def browser_failed(request: Request, exc: Exception) -> JSONResponse:
        """A browser failure maps from its `.kind`; one not listed is the server's fault."""
        # A launch error has no kind: no URL to attach one to.
        kind = getattr(exc, "kind", "timeout" if isinstance(exc, TimeoutError) else "launch_failed")
        if (url := getattr(exc, "url", None)) is not None:
            request.state.url = url
        return _error(request, STATUS.get(kind, 500), kind, str(exc))

    for cause in (onyxweb.OnyxwebError, TimeoutError):
        app.add_exception_handler(cause, browser_failed)

    @app.post(
        "/fetch",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": FetchRequest.model_json_schema()}},
            }
        },
    )
    async def fetch(request: Request) -> Response:
        try:
            data: Any = json.loads(await request.body())
        except ValueError:
            return _error(request, 422, "invalid_request", "the request body is not valid JSON.")
        if not isinstance(data, dict):
            return _error(
                request, 422, "invalid_request", "the request body must be a JSON object."
            )
        for name in REFUSED_FIELDS:
            if name in data:
                return _error(
                    request,
                    400,
                    "refused_field",
                    f"`{name}` is not accepted: the server never runs caller-supplied "
                    "JavaScript. Remove it and send the request again.",
                )
        try:
            wanted = FetchRequest.model_validate(data)
        except ValidationError as ve:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in ve.errors()
            )
            return _error(request, 422, "invalid_request", problems)
        request.state.url = wanted.url  # every handler below names it in its answer
        page = await core.fetch(
            wanted.url, FetchOptions(engine=wanted.engine, wait_ms=wanted.wait_ms)
        )
        body, headers = await asyncio.to_thread(
            _encoded, page, request.headers.get("accept-encoding", "")
        )
        return Response(body, media_type="application/json", headers=headers)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "engines": core.health(), "stats": core.stats()}

    return app
