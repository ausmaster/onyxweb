"""C15 HTTP API — a request maps to a status, a body shape, headers and an encoding.

``build_app(core)`` has four routes over one core: ``/fetch`` (a snapshot), ``/screenshot`` (the
image itself), ``/fetch_all`` (a snapshot and its image in one JSON object) and ``/batch`` (one
line of NDJSON per URL, in the order given). It is driven in-process with FastAPI's test client
over a fake browser (see conftest), so the tables run without Chrome. ``REQUESTS`` pin what a
good or bad request gets on any route: its body, the options that reach the browser, or an error
envelope that names the problem, never the refused JavaScript fields. ``FAILURES`` map each
failure a route can raise to one status and kind (a batch carries it in the URL's place), and
every case then serves a following request: an error never wedges the server. ``ENCODINGS`` pin
content negotiation, ``AUTH`` the token, and ``SCHEMAS`` the fields each route accepts. One test
per route carries a real page. The server holds nothing between requests.
"""

from __future__ import annotations

import base64
import json
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import onyxweb
import pytest
import zstandard
from conftest import PUBLIC
from fastapi.testclient import TestClient
from onyxweb.testing import FakeClientFactory
from onyxweb_server.core import CoreConfig, ServerCore
from onyxweb_server.http import build_app

OK: dict[str, Any] = {"url": PUBLIC + "ok"}
TWO: dict[str, Any] = {"urls": [PUBLIC + "one", PUBLIC + "two"]}
BODIES: dict[str, dict[str, Any]] = {
    "/fetch": OK,
    "/screenshot": OK,
    "/fetch_all": OK,
    "/batch": TWO,
}
_JS = "the server never runs caller-supplied JavaScript"
_ADS = ["*://*.ads.test/*"]
_AUTH = {"Authorization": "Bearer x"}
PNG = b"\x89PNG\r\n\x1a\n"
MAGIC = {"png": PNG, "jpeg": b"\xff\xd8\xff", "webp": b"RIFF"}  # how each format starts
# What a route's response holds, once its first line is parsed: the snapshot.
SNAPSHOTS = {
    "/fetch": lambda d: d,
    "/fetch_all": lambda d: d["snapshot"],
    "/batch": lambda d: d["snapshot"],
}


@dataclass(frozen=True)
class Request:
    """One request, and what the response must say."""

    body: dict[str, Any] | str
    status: int = 200
    kind: str | None = None  # the error envelope's kind, when it must be one
    says: tuple[str, ...] = ()  # fragments of the body (snapshot JSON or error message)
    engines: tuple[str, ...] = ("full",)  # engines the fake browser was built for
    headers: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)  # CoreConfig overrides
    then: int = 200  # the status of an ordinary request right after, on the same server
    asked: dict[str, Any] | None = None  # overrides each URL that reached the browser carried
    format: str = "png"  # the image a screenshot, or a fetch_all, comes back as
    lines: tuple[str, ...] = ()  # a batch's lines in order: "snapshot", or an error's kind
    path: str = "/fetch"
    names: str | None = None  # the URL an error envelope must name, when it must


_SHOT = {
    "engine": "shell",
    "wait_ms": 100,
    "timeout_ms": 5000,
    "wait_until": "load",
    "headers": _AUTH,
}
_SHOT_ASKED = {
    "wait_after_ms": 100,
    "timeout_ms": 5000,
    "wait_until": "load",
    "extra_headers": _AUTH,
}
_PAGE = {**_SHOT, "block_urls": _ADS, "bypass_anti_bot": True}
_PAGE_ASKED = {**_SHOT_ASKED, "block_urls": _ADS, "bypass_anti_bot": True}
_PRIVATE = "http://127.0.0.1/"

REQUESTS: dict[str, Request] = {
    # Options: each reaches the browser as the override it names, and a bad one is refused by name.
    "fetch_timeout_ms": Request({**OK, "timeout_ms": 5000}, asked={"timeout_ms": 5000}),
    "fetch_wait_until": Request(
        {**OK, "wait_until": "domcontentloaded"}, asked={"wait_until": "domcontentloaded"}
    ),
    "fetch_headers": Request({**OK, "headers": _AUTH}, asked={"extra_headers": _AUTH}),
    "fetch_block_urls": Request({**OK, "block_urls": _ADS}, asked={"block_urls": _ADS}),
    "fetch_bypass_anti_bot": Request(
        {**OK, "bypass_anti_bot": True}, asked={"bypass_anti_bot": True}
    ),
    "fetch_every_option_at_once": Request({**OK, **_PAGE}, engines=("shell",), asked=_PAGE_ASKED),
    "a_timeout_over_the_ceiling": Request(
        {**OK, "timeout_ms": 999_999},
        status=400,
        kind="invalid_request",
        says=("timeout_ms",),
        engines=(),
    ),
    "an_unknown_wait_until": Request(
        {**OK, "wait_until": "never"}, 400, "invalid_request", ("wait_until",), ()
    ),
    "a_header_chrome_computes": Request(
        {**OK, "headers": {"Host": "x"}}, 400, "invalid_request", ("cannot set 'Host'",), ()
    ),
    "too_many_block_patterns": Request(
        {**OK, "block_urls": ["*://a.test/*"] * 51}, 400, "invalid_request", ("block_urls",), ()
    ),
    "a_header_that_is_not_a_string": Request(
        {**OK, "headers": {"a": 1}}, 422, "invalid_request", ("headers",), ()
    ),
    # A screenshot is the image itself, so it is never re-encoded, whatever the caller accepts.
    "screenshot_is_a_png_by_default": Request(
        OK, headers={"accept-encoding": "zstd"}, asked={}, path="/screenshot"
    ),
    "screenshot_full_page": Request(
        {**OK, "full_page": True}, asked={"full_page": True}, path="/screenshot"
    ),
    "screenshot_in_jpeg": Request(
        {**OK, "format": "jpeg", "quality": 40},
        format="jpeg",
        asked={"format": "jpeg", "quality": 40},
        path="/screenshot",
    ),
    "screenshot_in_webp": Request(
        {**OK, "format": "webp"}, format="webp", asked={"format": "webp"}, path="/screenshot"
    ),
    "screenshot_viewport": Request(
        {**OK, "viewport": [640, 480]}, asked={"viewport": (640, 480)}, path="/screenshot"
    ),
    "screenshot_takes_the_fetch_options": Request(
        {**OK, **_SHOT}, engines=("shell",), asked=_SHOT_ASKED, path="/screenshot"
    ),
    "screenshot_of_a_private_address": Request(
        {"url": _PRIVATE}, 400, "invalid_request", ("private",), (), path="/screenshot"
    ),
    "screenshot_of_an_unknown_format": Request(
        {**OK, "format": "gif"}, 400, "invalid_request", ("format must be",), (), path="/screenshot"
    ),
    "screenshot_quality_over_100": Request(
        {**OK, "quality": 101},
        400,
        "invalid_request",
        ("quality must be",),
        (),
        path="/screenshot",
        names=OK["url"],
    ),
    "screenshot_viewport_of_zero": Request(
        {**OK, "viewport": [0, 480]},
        400,
        "invalid_request",
        ("viewport must be",),
        (),
        path="/screenshot",
    ),
    # An image has no blocked URLs or anti-bot wait to apply, so the route does not accept them.
    "screenshot_takes_no_block_urls": Request(
        {**OK, "block_urls": _ADS}, 422, "invalid_request", ("block_urls",), (), path="/screenshot"
    ),
    "screenshot_refuses_scripts": Request(
        {**OK, "scripts": ["x"]}, 400, "refused_field", ("scripts", _JS), (), path="/screenshot"
    ),
    "fetch_all_returns_the_page_and_its_image": Request(OK, asked={}, path="/fetch_all"),
    "fetch_all_in_webp": Request(
        {**OK, "format": "webp"}, format="webp", asked={"format": "webp"}, path="/fetch_all"
    ),
    "fetch_all_full_page": Request(
        {**OK, "full_page": True}, asked={"full_page": True}, path="/fetch_all"
    ),
    "fetch_all_takes_every_fetch_option": Request(
        {**OK, **_PAGE}, engines=("shell",), asked=_PAGE_ASKED, path="/fetch_all"
    ),
    "fetch_all_takes_no_viewport": Request(
        {**OK, "viewport": [640, 480]}, 422, "invalid_request", ("viewport",), (), path="/fetch_all"
    ),
    "fetch_all_of_a_private_address": Request(
        {"url": _PRIVATE},
        400,
        "invalid_request",
        ("private",),
        (),
        path="/fetch_all",
        names=_PRIVATE,
    ),
    "fetch_all_refuses_actions": Request(
        {**OK, "actions": []}, 400, "refused_field", ("actions", _JS), (), path="/fetch_all"
    ),
    "a_batch_is_a_line_per_url_in_the_order_given": Request(
        TWO, lines=("snapshot", "snapshot"), asked={}, path="/batch"
    ),
    "a_batch_applies_its_options_to_every_url": Request(
        {**TWO, **_PAGE},
        engines=("shell",),
        lines=("snapshot", "snapshot"),
        asked=_PAGE_ASKED,
        path="/batch",
    ),
    # One bad URL never fails the rest: it is a line of its own, in its place.
    "a_refused_url_is_a_line_in_its_place": Request(
        {"urls": [PUBLIC + "one", _PRIVATE, PUBLIC + "two"]},
        lines=("snapshot", "invalid_request", "snapshot"),
        asked={},
        path="/batch",
    ),
    "a_page_over_the_cap_is_a_line_in_its_place": Request(
        TWO, lines=("too_large", "too_large"), config={"max_page_bytes": 10}, path="/batch"
    ),
    "a_batch_of_no_urls": Request(
        {"urls": []}, 400, "invalid_request", ("urls holds 0",), (), path="/batch"
    ),
    "a_batch_over_the_limit": Request(
        {"urls": [PUBLIC + "x"] * 51}, 400, "invalid_request", ("urls holds 51",), (), path="/batch"
    ),
    "a_batch_needs_a_list": Request(
        {"urls": PUBLIC}, 422, "invalid_request", ("urls",), (), path="/batch"
    ),
    "a_batch_takes_no_url_field": Request(
        {**TWO, "url": PUBLIC + "x"}, 422, "invalid_request", ("url",), (), path="/batch"
    ),
    "a_batch_refuses_post_load_scripts": Request(
        {**TWO, "post_load_scripts": ["x"]},
        400,
        "refused_field",
        ("post_load_scripts", _JS),
        (),
        path="/batch",
    ),
    "a_body_sent_without_a_json_content_type": Request(
        '{"url": "http://93.184.216.34/ok"}',
        status=422,
        kind="invalid_request",
        says=("JSON object", "Content-Type: application/json"),
        engines=(),
    ),
    "a_fetch_returns_a_snapshot": Request(OK, says=("onyxweb_snapshot", PUBLIC + "ok", "<body>")),
    "the_shell_engine": Request({**OK, "engine": "shell"}, engines=("shell",)),
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
    "a_body_that_is_not_json": Request(
        "not json",
        422,
        "invalid_request",
        ("not valid JSON",),
        (),
        headers={"content-type": "application/json"},
    ),
    # The core's own refusals reach HTTP as 400s with the same message MCP shows.
    "a_private_address": Request(
        {"url": "http://127.0.0.1/"},
        400,
        "invalid_request",
        ("private",),
        (),
        names="http://127.0.0.1/",
    ),
    "a_file_url": Request({"url": "file:///etc/passwd"}, 400, "invalid_request", ("http",), ()),
    "a_settle_over_the_ceiling": Request(
        {**OK, "wait_ms": 999_999}, 400, "invalid_request", ("wait_ms",), (), names=OK["url"]
    ),
    "an_unknown_engine": Request(
        {**OK, "engine": "turbo"}, 400, "invalid_request", ("'shell'",), ()
    ),
    # The core's size limit is a 413 with its own kind, so a caller can tell it from a bad request.
    "a_page_over_the_size_cap": Request(
        OK,
        413,
        "too_large",
        ("bytes", "ONYXWEB_SERVER_MAX_PAGE_BYTES"),
        ("full",),  # the page was fetched before it was refused
        config={"max_page_bytes": 10},
        then=413,  # the cap still applies; what matters is that the server answers
    ),
}


def _client(
    factory: FakeClientFactory | None = None, config: dict[str, Any] | None = None
) -> tuple[TestClient, ServerCore, FakeClientFactory]:
    factory = factory or FakeClientFactory()
    core = ServerCore(factory, config=CoreConfig(**(config or {})))
    return TestClient(build_app(core)), core, factory


def _post(
    client: TestClient,
    body: dict[str, Any] | str,
    headers: dict[str, str] | None = None,
    path: str = "/fetch",
) -> Any:
    if isinstance(body, str):
        return client.post(path, content=body, headers=headers)
    return client.post(path, json=body, headers=headers)


@pytest.mark.parametrize("name", list(REQUESTS))
def test_request(name: str) -> None:
    row = REQUESTS[name]
    client, core, factory = _client(config=row.config)
    with client:
        r = _post(client, row.body, headers=row.headers, path=row.path)
        assert r.status_code == row.status, r.text[:300]
        if row.kind is not None:
            assert r.headers["content-type"].startswith("application/json")
            assert r.json()["error"]["kind"] == row.kind
            if row.names is not None:
                assert r.json()["error"]["url"] == row.names, r.text
        elif row.path == "/screenshot":
            assert r.headers["content-type"] == f"image/{row.format}"
            assert "content-encoding" not in r.headers
            assert r.content.startswith(MAGIC[row.format])
        elif row.path == "/batch":
            assert r.headers["content-type"].startswith("application/x-ndjson")
            got = [json.loads(line) for line in r.text.splitlines()]
            assert [g["url"] for g in got] == row.body["urls"]  # type: ignore[index]
            assert [g["error"]["kind"] if "error" in g else "snapshot" for g in got] == list(
                row.lines
            )
            assert all(
                g["snapshot"]["onyxweb_snapshot"] == onyxweb.SNAPSHOT_VERSION
                for g in got
                if "snapshot" in g
            )
        else:
            assert r.headers["content-type"].startswith("application/json")
            assert SNAPSHOTS[row.path](r.json())["onyxweb_snapshot"] == onyxweb.SNAPSHOT_VERSION
            if row.path == "/fetch_all":
                assert row.format == r.json()["format"]
                assert base64.b64decode(r.json()["image"]).startswith(MAGIC[row.format])
        for fragment in row.says:
            assert fragment in r.text, r.text[:300]
        assert factory.engines == list(row.engines), "clients built beyond what the row says"
        if row.asked is not None:
            fetched = [call for _, built in factory.built for call in built.fetched]
            assert fetched, "nothing reached the browser"
            assert all(overrides == row.asked for _, overrides in fetched), fetched
        assert core.pages() == [], "the server held a page between requests"
        # A refusal never wedges the server.
        assert _post(client, BODIES[row.path], path=row.path).status_code == row.then


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
    # A kind the server does not know is its own fault, not the caller's.
    "an_unlisted_kind": (_failure("brand_new_kind"), 500, "brand_new_kind"),
    "chrome_not_found": (_failure("chrome_not_found"), 503, "chrome_not_found"),
    # A launch error carries no kind (there is no URL to attach one to), and still maps.
    "launch_failed": (_failure(None), 503, "launch_failed"),
}


@pytest.mark.parametrize("path", list(BODIES))
@pytest.mark.parametrize("name", list(FAILURES))
def test_failure(name: str, path: str) -> None:
    err, status, kind = FAILURES[name]
    client, _, factory = _client(FakeClientFactory(error=err))
    with client:
        r = _post(client, BODIES[path], path=path)
        if path == "/batch":  # a batch never fails as a whole: each URL carries its own failure
            assert r.status_code == 200, r.text
            errors = [json.loads(line)["error"] for line in r.text.splitlines()]
            assert [e["kind"] for e in errors] == [kind, kind]
            assert all("boom" in e["message"] for e in errors)
        else:
            assert (r.status_code, r.json()["error"]["kind"]) == (status, kind), r.text
            assert "boom" in r.json()["error"]["message"]
        factory.built[0][1].error = None
        ok = _post(client, BODIES[path], path=path)
        assert ok.status_code == 200, "an error left the server unable to serve"


# Accept-Encoding -> the Content-Encoding the response must carry, or None for identity.
ENCODINGS: dict[str, tuple[str | None, str | None]] = {
    "zstd_when_accepted": ("zstd", "zstd"),
    "zstd_among_others": ("gzip, deflate, zstd", "zstd"),
    "identity_when_nothing_is_offered": (None, None),
    "identity_for_an_unrelated_coding": ("gzip", None),
    "identity_when_zstd_is_refused": ("zstd;q=0", None),
}


@pytest.mark.parametrize("path", list(SNAPSHOTS))
@pytest.mark.parametrize("name", list(ENCODINGS))
def test_encoding(name: str, path: str) -> None:
    offered, expected = ENCODINGS[name]
    client, _, _ = _client()
    with client:
        headers = {"accept-encoding": offered} if offered else {"accept-encoding": "identity"}
        r = client.post(path, json=BODIES[path], headers=headers)
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == expected
    assert "accept-encoding" in r.headers["vary"].lower()
    first = SNAPSHOTS[path](json.loads(r.text.splitlines()[0]))
    assert first["onyxweb_snapshot"] == onyxweb.SNAPSHOT_VERSION


@pytest.mark.parametrize("path", list(SNAPSHOTS))
def test_zstd_actually_compresses(path: str) -> None:
    """Content-Encoding says zstd only when the bytes are zstd: decode them by hand.

    New test: the client library decodes transparently, so only raw bytes can show it. A batch
    is compressed as it is written, one line at a time, and must still be one valid stream.
    """
    client, _, _ = _client()
    with (
        client,
        client.stream("POST", path, json=BODIES[path], headers={"accept-encoding": "zstd"}) as r,
    ):
        raw = b"".join(r.iter_raw())
    plain = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    assert SNAPSHOTS[path](json.loads(plain.splitlines()[0]))["final_url"].startswith(PUBLIC)
    assert len(raw) < len(plain)
    if path == "/batch":
        assert len(plain.splitlines()) == 2, "the stream lost a line"


def test_health_reports_each_built_engine_and_the_counters() -> None:
    """``/health`` lists the engines built so far, whether each Chrome is alive, and the counters.

    New test: it is a second route, and no request table above reaches it.
    """
    client, _, factory = _client()
    with client:
        first = client.get("/health").json()
        assert (first["status"], first["engines"]) == ("ok", {})
        assert first["stats"]["requests"] == 0
        _post(client, OK)
        _post(client, {"url": "http://127.0.0.1/"})  # refused by the guard
        second = client.get("/health").json()
        assert second["engines"] == {"full": True}
        assert second["stats"]["requests"] == 2
        assert second["stats"]["failures"] == {"refused_url": 1}
        factory.built[0][1].die()
        assert client.get("/health").json()["engines"] == {"full": False}


@pytest.mark.parametrize("path", ["/fetch", "/fetch_all", "/batch", "/screenshot"])
def test_a_real_page_over_http(bucket_page: str, path: str) -> None:
    """A real page through each route: a snapshot loads back as a full ``RenderResult``, and an
    image is a PNG of the size asked for.

    New test: every row above runs on a fake browser, and this one needs the real one.
    """
    # The test server listens on 127.0.0.1, which the guard and the egress proxy both refuse.
    core = ServerCore(url_guard=lambda url: None, config=CoreConfig(egress=False))
    body: dict[str, Any] = {"urls": [bucket_page]} if path == "/batch" else {"url": bucket_page}
    if path == "/screenshot":
        body["viewport"] = [640, 480]
    with TestClient(build_app(core)) as client:
        r = client.post(path, json=body, headers={"accept-encoding": "zstd"})
    assert r.status_code == 200, r.text[:300]
    if path == "/screenshot":
        assert r.content.startswith(PNG)
        assert struct.unpack(">II", r.content[16:24]) == (640, 480)
    else:
        first = json.loads(r.text.splitlines()[0])
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / "page.json"
            saved.write_text(json.dumps(SNAPSHOTS[path](first)))
            page = onyxweb.RenderResult.load(saved)
        assert (page.title, len(page.scripts), page.status_code) == ("Bucket Fixture", 4, 200)
        assert page.final_url == bucket_page
        if path == "/fetch_all":
            assert base64.b64decode(first["image"]).startswith(PNG)
    assert core.pages() == []


_KNOBS = {"engine", "wait_ms", "timeout_ms", "wait_until", "headers"}
_IMAGE = {"full_page", "format", "quality"}
SCHEMAS: dict[str, set[str]] = {
    "/fetch": {"url", "block_urls", "bypass_anti_bot"} | _KNOBS,
    "/fetch_all": {"url", "block_urls", "bypass_anti_bot"} | _IMAGE | _KNOBS,
    "/screenshot": {"url", "viewport"} | _IMAGE | _KNOBS,
    "/batch": {"urls", "block_urls", "bypass_anti_bot"} | _KNOBS,
}


@pytest.mark.parametrize("path", list(SCHEMAS))
def test_the_app_offers_no_script_execution(path: str) -> None:
    """The published request schema lists exactly the fields the route accepts.

    New test: the refusal is an absence, which only the published schema can show.
    """
    client, _, _ = _client()
    with client:
        schema = client.get("/openapi.json").json()
    ref = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    body = schema["components"]["schemas"][ref["$ref"].rsplit("/", 1)[1]]
    assert set(body["properties"]) == SCHEMAS[path]
    assert body["additionalProperties"] is False
    assert {"scripts", "post_load_scripts", "actions"}.isdisjoint(body["properties"])


@dataclass(frozen=True)
class Auth:
    """The token the server was given, what a caller sends, and the status it earns."""

    token: str | None
    header: str | None
    status: int = 200
    via_env: bool = False  # the token comes from ONYXWEB_SERVER_TOKEN, not an argument


AUTH: dict[str, Auth] = {
    "no_token_configured_is_open": Auth(None, None),
    "the_right_token": Auth("s3cret", "Bearer s3cret"),
    "the_scheme_is_case_insensitive": Auth("s3cret", "bearer s3cret"),
    "an_empty_token_is_no_token": Auth("", None),
    "a_token_from_the_environment": Auth("s3cret", "Bearer s3cret", via_env=True),
    "no_header": Auth("s3cret", None, 401),
    "a_wrong_token": Auth("s3cret", "Bearer nope", 401),
    "a_prefix_of_the_token": Auth("s3cret", "Bearer s3cre", 401),
    "the_token_without_a_scheme": Auth("s3cret", "s3cret", 401),
    "another_scheme": Auth("s3cret", "Basic czNjcmV0", 401),
    "the_right_token_under_another_scheme": Auth("s3cret", "Basic s3cret", 401),
    "an_empty_bearer": Auth("s3cret", "Bearer ", 401),
    "the_environment_token_is_enforced": Auth("s3cret", None, 401, via_env=True),
}


@pytest.mark.parametrize("path", list(BODIES))
@pytest.mark.parametrize("name", list(AUTH))
def test_a_token_gates_every_route_but_health(
    monkeypatch: pytest.MonkeyPatch, name: str, path: str
) -> None:
    """A configured token guards every route, and a caller without it never reaches the browser.

    New test: the rows above vary the request, and this one varies who sends it.
    """
    row = AUTH[name]
    monkeypatch.delenv("ONYXWEB_SERVER_TOKEN", raising=False)
    factory = FakeClientFactory()
    core = ServerCore(factory)
    if row.via_env:
        monkeypatch.setenv("ONYXWEB_SERVER_TOKEN", row.token or "")
        app = build_app(core)
    else:
        app = build_app(core, token=row.token)
    headers = {"authorization": row.header} if row.header is not None else {}
    with TestClient(app) as client:
        r = client.post(path, json=BODIES[path], headers=headers)
        assert r.status_code == row.status, r.text[:200]
        if row.status == 401:
            assert r.json()["error"]["kind"] == "unauthorized"
            assert r.headers["www-authenticate"] == "Bearer"
            assert "s3cret" not in r.text
            assert factory.built == [] and core.stats()["requests"] == 0
        assert client.get("/health", headers=headers).status_code == 200  # a liveness probe
