"""C15 HTTP API — a request maps to a status, a body shape, headers and an encoding.

``build_app(core)`` is driven in-process with FastAPI's test client over a fake browser (see
conftest), so the tables run without Chrome. ``REQUESTS`` pin what a good or bad request gets:
a snapshot, or an error envelope that names the problem, never the refused JavaScript fields.
``FAILURES`` map each failure a fetch can raise to one status and kind, and every case then
serves a following request: an error never wedges the server. ``ENCODINGS`` pin content
negotiation. One test fetches a real page through the app and loads the snapshot back, to prove
the wire format rebuilds a full ``RenderResult``. The server holds nothing between requests.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import onyxweb
import pytest
import zstandard
from conftest import PUBLIC, Factory
from fastapi.testclient import TestClient
from onyxweb_server.core import ServerCore
from onyxweb_server.http import build_app

OK = {"url": PUBLIC + "ok"}
_JS = "the server never runs caller-supplied JavaScript"


@dataclass(frozen=True)
class Request:
    """One request, and what the response must say."""

    body: dict[str, Any] | str
    status: int = 200
    kind: str | None = None  # the error envelope's kind, when it must be one
    says: tuple[str, ...] = ()  # fragments of the body (snapshot JSON or error message)
    engines: tuple[str, ...] = ("shell",)  # engines the fake browser was built for
    headers: dict[str, str] = field(default_factory=dict)


REQUESTS: dict[str, Request] = {
    "a_fetch_returns_a_snapshot": Request(OK, says=("onyxweb_snapshot", PUBLIC + "ok", "<body>")),
    "the_full_engine": Request({**OK, "engine": "full"}, engines=("full",)),
    "a_settle_within_the_ceiling": Request({**OK, "wait_ms": 500}),
    # Refused fields are named, so a caller never believes its script ran.
    "scripts_are_refused": Request(
        {**OK, "scripts": ["x"]}, 400, "refused_field", ("scripts", _JS), ()
    ),
    "post_load_scripts_are_refused": Request(
        {**OK, "post_load_scripts": ["x"]}, 400, "refused_field", ("post_load_scripts", _JS), ()
    ),
    "actions_are_refused": Request(
        {**OK, "actions": []}, 400, "refused_field", ("actions", _JS), ()
    ),
    "an_unknown_field_is_named": Request(
        {**OK, "bogus": 1}, 422, "invalid_request", ("bogus",), ()
    ),
    "a_missing_url_is_named": Request({}, 422, "invalid_request", ("url",), ()),
    "a_body_that_is_not_json": Request("not json", 422, "invalid_request", ("JSON",), ()),
    # The core's own refusals reach HTTP as 400s with the same message MCP shows.
    "a_private_address": Request(
        {"url": "http://127.0.0.1/"}, 400, "invalid_request", ("private",), ()
    ),
    "a_file_url": Request({"url": "file:///etc/passwd"}, 400, "invalid_request", ("http",), ()),
    "a_settle_over_the_ceiling": Request(
        {**OK, "wait_ms": 999_999}, 400, "invalid_request", ("wait_ms",), ()
    ),
    "an_unknown_engine": Request(
        {**OK, "engine": "turbo"}, 400, "invalid_request", ("'shell'",), ()
    ),
}


def _client(factory: Factory | None = None) -> tuple[TestClient, ServerCore, Factory]:
    factory = factory or Factory()
    core = ServerCore(factory)
    return TestClient(build_app(core)), core, factory


def _post(
    client: TestClient, body: dict[str, Any] | str, headers: dict[str, str] | None = None
) -> Any:
    if isinstance(body, str):
        return client.post(
            "/fetch", content=body, headers={"content-type": "application/json", **(headers or {})}
        )
    return client.post("/fetch", json=body, headers=headers)


@pytest.mark.parametrize("name", list(REQUESTS))
def test_request(name: str) -> None:
    row = REQUESTS[name]
    client, core, factory = _client()
    with client:
        r = _post(client, row.body, headers=row.headers)
        assert r.status_code == row.status, r.text
        assert r.headers["content-type"].startswith("application/json")
        for fragment in row.says:
            assert fragment in r.text, r.text[:300]
        if row.kind is None:
            assert r.json()["onyxweb_snapshot"] == onyxweb.SNAPSHOT_VERSION
            assert factory.engines == list(row.engines)
        else:
            assert r.json()["error"]["kind"] == row.kind
            assert factory.built == [], "a client was built for a request the server refuses"
        assert core.pages() == [], "the server held a page between requests"
        assert _post(client, OK).status_code == 200  # a refusal never wedges the server


def _failure(kind: str | None, cls: type[BaseException] = onyxweb.OnyxwebError) -> BaseException:
    err = cls("boom")
    if kind is not None:
        err.kind = kind  # type: ignore[attr-defined]
    err.url = PUBLIC + "ok"  # type: ignore[attr-defined]
    return err


# What a fetch can raise -> the status and kind the caller sees.
FAILURES: dict[str, tuple[BaseException, int, str]] = {
    "chrome_exited": (_failure("chrome_exited", onyxweb.ChromeExitedError), 503, "chrome_exited"),
    "queue_timeout": (_failure("queue_timeout", onyxweb.QueueTimeoutError), 503, "queue_timeout"),
    "navigation_timeout": (_failure("navigation_timeout", TimeoutError), 504, "navigation_timeout"),
    "timeout": (_failure("timeout", TimeoutError), 504, "timeout"),
    "cdp": (_failure("cdp"), 502, "cdp"),
    "io": (_failure("io"), 502, "io"),
    "invalid_url": (_failure("invalid_url"), 400, "invalid_url"),
    "invalid_config": (_failure("invalid_config"), 400, "invalid_config"),
    "post_load_script": (_failure("post_load_script"), 400, "post_load_script"),
    "internal": (_failure("internal"), 500, "internal"),
    "chrome_not_found": (_failure("chrome_not_found"), 503, "chrome_not_found"),
    # A launch error carries no kind (there is no URL to attach one to), and still maps.
    "launch_failed": (_failure(None), 503, "launch_failed"),
}


@pytest.mark.parametrize("name", list(FAILURES))
def test_failure(name: str) -> None:
    err, status, kind = FAILURES[name]
    client, _, factory = _client(Factory(error=err))
    with client:
        r = _post(client, OK)
        assert (r.status_code, r.json()["error"]["kind"]) == (status, kind), r.text
        assert "boom" in r.json()["error"]["message"]
        factory.built[0][1].error = None
        assert _post(client, OK).status_code == 200, "an error left the server unable to serve"


# Accept-Encoding -> the Content-Encoding the response must carry, or None for identity.
ENCODINGS: dict[str, tuple[str | None, str | None]] = {
    "zstd_when_accepted": ("zstd", "zstd"),
    "zstd_among_others": ("gzip, deflate, zstd", "zstd"),
    "identity_when_nothing_is_offered": (None, None),
    "identity_for_an_unrelated_coding": ("gzip", None),
    "identity_when_zstd_is_refused": ("zstd;q=0", None),
}


@pytest.mark.parametrize("name", list(ENCODINGS))
def test_encoding(name: str) -> None:
    offered, expected = ENCODINGS[name]
    client, _, _ = _client()
    body = {"url": PUBLIC + "ok"}
    with client:
        headers = {"accept-encoding": offered} if offered else {"accept-encoding": "identity"}
        r = client.post("/fetch", json=body, headers=headers)
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == expected
    assert "accept-encoding" in r.headers["vary"].lower()
    assert r.json()["onyxweb_snapshot"] == onyxweb.SNAPSHOT_VERSION


def test_zstd_actually_compresses() -> None:
    """Content-Encoding says zstd only when the bytes are zstd: decode them by hand.

    New test: the client library decodes transparently, so only raw bytes can show it.
    """
    client, _, _ = _client()
    with client, client.stream("POST", "/fetch", json=OK, headers={"accept-encoding": "zstd"}) as r:
        raw = b"".join(r.iter_raw())
    plain = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    assert json.loads(plain)["final_url"] == PUBLIC + "ok"
    assert len(raw) < len(plain)


def test_health_reports_each_built_engine() -> None:
    """``/health`` lists the engines built so far and whether each one's Chrome is alive.

    New test: it is a second route, and no request table above reaches it.
    """
    client, _, factory = _client()
    with client:
        assert client.get("/health").json() == {"status": "ok", "engines": {}}
        _post(client, OK)
        assert client.get("/health").json() == {"status": "ok", "engines": {"shell": True}}
        factory.built[0][1].alive = False
        assert client.get("/health").json() == {"status": "ok", "engines": {"shell": False}}


def test_a_snapshot_over_http_rebuilds_the_page(bucket_page: str) -> None:
    """A real fetch through the app loads back as a full ``RenderResult``.

    New test: every row above runs on a fake browser, and this one needs the real one.
    """
    core = ServerCore(url_guard=lambda url: None)  # the test server listens on 127.0.0.1
    with TestClient(build_app(core)) as client:
        r = client.post("/fetch", json={"url": bucket_page}, headers={"accept-encoding": "zstd"})
    assert r.status_code == 200
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "page.json"
        path.write_bytes(r.content)
        page = onyxweb.RenderResult.load(path)
    assert (page.title, len(page.scripts), page.status_code) == ("Bucket Fixture", 4, 200)
    assert page.final_url == bucket_page
    assert core.pages() == []


def test_the_app_offers_no_script_execution() -> None:
    """The published request schema lists exactly the fields the server accepts.

    New test: the refusal is an absence, which only the published schema can show.
    """
    client, _, _ = _client()
    with client:
        schema = client.get("/openapi.json").json()
    body = schema["paths"]["/fetch"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert set(body["properties"]) == {"url", "engine", "wait_ms"}
    assert body["additionalProperties"] is False
