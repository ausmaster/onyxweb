"""The HTTP front-end: one route per browser operation, whole snapshots on the wire.

A program calling this wants the page, not a digest of it, so the answer to ``POST /fetch``
is `RenderResult.snapshot()` as JSON, zstd-compressed when the caller accepts it, and
``RenderResult.load`` rebuilds the page from it. The server holds nothing between requests.
Every request goes through `ServerCore.fetch`, so the URL guard and the ceilings apply as they
do for MCP, and the fields that would run caller JavaScript are refused by name.

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

from onyxweb_server.core import FetchOptions, Refused, ServerCore

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


def _error(status: int, kind: str, message: str, url: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"kind": kind, "message": message, "url": url}}, status_code=status
    )


def _accepts_zstd(header: str) -> bool:
    """Whether an ``Accept-Encoding`` value offers zstd with a weight above zero."""
    for part in header.split(","):
        coding, *params = (piece.strip() for piece in part.split(";"))
        if coding.lower() != "zstd":
            continue
        try:
            weight = next((float(p[2:]) for p in params if p.lower().startswith("q=")), 1.0)
        except ValueError:
            return False
        return weight > 0
    return False


def _encode(page: onyxweb.RenderResult, compress: bool) -> bytes:
    body = json.dumps(page.snapshot(), ensure_ascii=False).encode()
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(body) if compress else body


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
    schema = {
        "required": True,
        "content": {"application/json": {"schema": FetchRequest.model_json_schema()}},
    }

    @app.post("/fetch", openapi_extra={"requestBody": schema})
    async def fetch(request: Request) -> Response:
        try:
            data: Any = json.loads(await request.body())
        except ValueError:
            return _error(422, "invalid_request", "the request body is not valid JSON.")
        if not isinstance(data, dict):
            return _error(422, "invalid_request", "the request body must be a JSON object.")
        for name in REFUSED_FIELDS:
            if name in data:
                return _error(
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
            return _error(422, "invalid_request", problems)
        try:
            page = await core.fetch(
                wanted.url, FetchOptions(engine=wanted.engine, wait_ms=wanted.wait_ms)
            )
        except Refused as refusal:
            if refusal.code == "too_large":
                return _error(413, "too_large", str(refusal), wanted.url)
            return _error(400, "invalid_request", str(refusal), wanted.url)
        except ValueError as ve:
            return _error(400, "invalid_request", str(ve), wanted.url)
        except (onyxweb.OnyxwebError, TimeoutError) as err:
            # A launch error has no kind: no URL to attach one to.
            kind = getattr(
                err, "kind", "timeout" if isinstance(err, TimeoutError) else "launch_failed"
            )
            return _error(STATUS.get(kind, 500), kind, str(err), getattr(err, "url", wanted.url))
        compress = _accepts_zstd(request.headers.get("accept-encoding", ""))
        body = await asyncio.to_thread(_encode, page, compress)
        headers = {"Vary": "Accept-Encoding"}
        if compress:
            headers["Content-Encoding"] = "zstd"
        return Response(body, media_type="application/json", headers=headers)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "engines": core.health(), "stats": core.stats()}

    return app
