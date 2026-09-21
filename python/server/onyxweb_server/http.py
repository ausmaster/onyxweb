"""The HTTP front-end: one route per browser operation, whole pages on the wire.

A program calling this wants the page, not a digest of it. ``POST /fetch`` answers with
`RenderResult.snapshot()` as JSON, and ``RenderResult.load`` rebuilds the page from it.
``POST /screenshot`` answers with the image itself. ``POST /fetch_all`` answers with one JSON
object holding both, the image as base64. ``POST /batch`` answers with one line of JSON per URL,
in the order given, each a snapshot or that URL's own failure. JSON is compressed with zstd
when the caller accepts it. The server holds nothing between requests.

Every route goes through `ServerCore`, so the URL guard and the ceilings apply as they do for
MCP. A request model lists every field its route accepts and forbids the rest, and the fields
that would run caller JavaScript are refused by name. A route reads as its happy path: what the
core refuses, the browser fails at, or the caller sends wrongly is turned into an answer by the
handlers registered in `build_app`, so every route maps a cause the same way.

Set ``ONYXWEB_SERVER_TOKEN`` to require ``Authorization: Bearer <token>`` on every route but
``GET /health``. ``onyxweb-server http`` binds loopback by default and refuses another address
unless a token is set.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

try:
    import zstandard
    from fastapi import APIRouter, Depends, FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response, StreamingResponse
except ImportError as ie:
    raise ImportError(
        "onyxweb_server.http needs fastapi and zstandard; "
        'install them with pip install "onyxweb-server[http]".'
    ) from ie

import onyxweb

from onyxweb_server.core import FetchOptions, Refused, ServerCore, ShotOptions, TooLarge

TOKEN_VAR: Final = "ONYXWEB_SERVER_TOKEN"
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
FETCH_KNOBS: Final = {f.name for f in dataclasses.fields(FetchOptions)}
SHOT_KNOBS: Final = {f.name for f in dataclasses.fields(ShotOptions)}


class _Timing(BaseModel):
    """What every route accepts. Fields are named as the core's `FetchOptions` names them."""

    model_config = ConfigDict(extra="forbid")

    engine: str = "shell"
    wait_ms: int = 0
    timeout_ms: int | None = None
    wait_until: str | None = None
    headers: dict[str, str] = {}

    def options(self) -> FetchOptions:
        """The core's options for this request: those of its fields the core knows."""
        return FetchOptions(**self.model_dump(include=FETCH_KNOBS))


class _Page(_Timing):
    """What a route that fetches the page also accepts. An image has neither to apply."""

    block_urls: list[str] = []
    bypass_anti_bot: bool | None = None


class _Image(BaseModel):
    """What a route that returns an image accepts."""

    model_config = ConfigDict(extra="forbid")

    full_page: bool = False
    format: str = "png"
    quality: int | None = None


class FetchRequest(_Page):
    """The body of ``POST /fetch``."""

    url: str


class FetchAllRequest(FetchRequest, _Image):
    """The body of ``POST /fetch_all``; the page is captured at the client's viewport."""


class ScreenshotRequest(_Timing, _Image):
    """The body of ``POST /screenshot``."""

    url: str
    viewport: tuple[int, int] | None = None


class BatchRequest(_Page):
    """The body of ``POST /batch``: the options apply to every URL."""

    urls: list[str]


class _Unauthorized(Exception):
    """A caller that did not present the token."""


def _problem(failure: BaseException) -> tuple[int, str]:
    """The status and kind a failure is answered with, in a response or in a batch's line."""
    if isinstance(failure, TooLarge):
        return 413, "too_large"
    if isinstance(failure, Refused):
        return 400, "invalid_request"
    # A launch error has no kind: there is no URL to attach one to.
    kind = getattr(
        failure, "kind", "timeout" if isinstance(failure, TimeoutError) else "launch_failed"
    )
    return STATUS.get(kind, 500), kind


def _error(
    request: Request, status: int, kind: str, message: str, url: str | None = None
) -> JSONResponse:
    """One error body, naming the URL the request was about when it has one."""
    url = url or getattr(request.state, "url", None)
    return JSONResponse(
        {"error": {"kind": kind, "message": message, "url": url}}, status_code=status
    )


def _accepts_zstd(request: Request) -> bool:
    """Whether the ``Accept-Encoding`` offers zstd with a weight above zero."""
    for part in request.headers.get("accept-encoding", "").split(","):
        coding, *params = (piece.strip() for piece in part.split(";"))
        if coding.lower() != "zstd":
            continue
        try:
            weight = next((float(p[2:]) for p in params if p.lower().startswith("q=")), 1.0)
        except ValueError:
            return False
        return weight > 0
    return False


def _wire(make: Callable[[], Any], compress: bool) -> tuple[bytes, dict[str, str]]:
    """The JSON `make` returns as it goes on the wire, with its headers.

    Runs in a thread, since building a large page's snapshot and compressing it hold the loop.
    """
    body = json.dumps(make(), ensure_ascii=False).encode()
    headers = {"Vary": "Accept-Encoding"}
    if compress:
        return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(body), headers | {
            "Content-Encoding": "zstd"
        }
    return body, headers


def build_app(core: ServerCore | None = None, token: str | None = None) -> FastAPI:
    """Build the HTTP app over `core`, or over a default core when none is given.

    Args:
        core: The core every route serves through.
        token: The bearer token every route but ``/health`` requires. Default: the value of
            ``ONYXWEB_SERVER_TOKEN``; none set, or an empty one, leaves the routes open.
    """
    core = core or ServerCore()
    token = os.environ.get(TOKEN_VAR) if token is None else token

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await core.aclose()

    async def authorized(request: Request) -> None:
        """Refuse a caller that does not present the token, when there is one to present."""
        if not token:
            return
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(given.encode(), token.encode()):
            raise _Unauthorized

    app = FastAPI(title="onyxweb-server", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(_Unauthorized)
    async def unauthorized(request: Request, exc: _Unauthorized) -> JSONResponse:
        response = _error(
            request, 401, "unauthorized", "send the token as `Authorization: Bearer <token>`."
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A body the route cannot read: a refused field is named, and the rest are 422."""
        problems = exc.errors()
        for problem in problems:
            if problem["type"] == "extra_forbidden" and problem["loc"][-1] in REFUSED_FIELDS:
                return _error(
                    request,
                    400,
                    "refused_field",
                    f"`{problem['loc'][-1]}` is not accepted: the server never runs "
                    "caller-supplied JavaScript. Remove it and send the request again.",
                )
        kinds = {problem["type"] for problem in problems}
        if "json_invalid" in kinds:
            message = "the request body is not valid JSON."
        elif "model_attributes_type" in kinds:  # a list, or JSON sent without its content type
            message = (
                "the request body must be a JSON object, sent with Content-Type: application/json."
            )
        else:
            message = "; ".join(
                f"{'.'.join(str(p) for p in problem['loc'])}: {problem['msg']}"
                for problem in problems
            )
        return _error(request, 422, "invalid_request", message)

    async def failed(request: Request, exc: Exception) -> JSONResponse:
        """A refusal or a browser failure, answered from the one mapping in `_problem`."""
        status, kind = _problem(exc)
        return _error(request, status, kind, str(exc), getattr(exc, "url", None))

    for cause in (Refused, onyxweb.OnyxwebError, TimeoutError):
        app.add_exception_handler(cause, failed)

    guarded = APIRouter(dependencies=[Depends(authorized)])

    @guarded.post("/fetch")
    async def fetch(wanted: FetchRequest, request: Request) -> Response:
        request.state.url = wanted.url  # the handlers above name it in their answers
        page = await core.fetch(wanted.url, wanted.options())
        body, headers = await asyncio.to_thread(_wire, page.snapshot, _accepts_zstd(request))
        return Response(body, media_type="application/json", headers=headers)

    @guarded.post("/screenshot")
    async def screenshot(wanted: ScreenshotRequest, request: Request) -> Response:
        request.state.url = wanted.url
        image = await core.screenshot(
            wanted.url, wanted.options(), ShotOptions(**wanted.model_dump(include=SHOT_KNOBS))
        )
        return Response(image, media_type=f"image/{wanted.format}")

    @guarded.post("/fetch_all")
    async def fetch_all(wanted: FetchAllRequest, request: Request) -> Response:
        request.state.url = wanted.url
        both = await core.fetch_all(
            wanted.url, wanted.options(), ShotOptions(**wanted.model_dump(include=SHOT_KNOBS))
        )

        def envelope() -> dict[str, Any]:
            return {
                "snapshot": both.html.snapshot(),
                "image": base64.b64encode(both.png).decode(),
                "format": wanted.format,
            }

        body, headers = await asyncio.to_thread(_wire, envelope, _accepts_zstd(request))
        return Response(body, media_type="application/json", headers=headers)

    @guarded.post("/batch")
    async def batch(wanted: BatchRequest, request: Request) -> StreamingResponse:
        results = await core.batch(wanted.urls, wanted.options())
        compress = _accepts_zstd(request)

        def line(url: str, item: onyxweb.RenderResult | Exception) -> bytes:
            if isinstance(item, Exception):
                _, kind = _problem(item)
                error = {"kind": kind, "message": str(item), "url": getattr(item, "url", url)}
                entry: dict[str, Any] = {"url": url, "error": error}
            else:
                entry = {"url": url, "snapshot": item.snapshot()}
            return json.dumps(entry, ensure_ascii=False).encode() + b"\n"

        async def lines() -> AsyncIterator[bytes]:
            """Each URL's line as it is built, so no more than one is held encoded at a time."""
            packer = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compressobj() if compress else None
            for url, item in zip(wanted.urls, results, strict=True):
                data = await asyncio.to_thread(line, url, item)
                if chunk := packer.compress(data) if packer else data:
                    yield chunk
            if packer:
                yield packer.flush()

        headers = {"Vary": "Accept-Encoding"}
        if compress:
            headers["Content-Encoding"] = "zstd"
        return StreamingResponse(lines(), media_type="application/x-ndjson", headers=headers)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "engines": core.health(), "stats": core.stats()}

    app.include_router(guarded)
    return app
