"""C3 config — a knob through any entry path validates, lands, and takes effect.

The entry paths are ``ClientConfig.from_flat`` (what ``Client(**kw)`` runs),
``ONYXWEB_*`` environment variables, ``update_config(**kw)``,
``update_config(config=...)``, assignment through the live ``client.config`` view,
and the presets, which are bundles of flat kwargs. The tables:

- ``FLAT_KWARGS``: every flat kwarg lands at its nested path on every entry path.
- ``DEFAULTS``: what each model holds when nothing is set.
- ``INVALID``: bad input raises the named exception with a message naming the fix;
  ``ACCEPTED`` holds input beside those rules that must pass unchanged.
- ``LAUNCH_ONLY``: a field fixed once Chrome runs refuses a runtime change and stays put.
- ``PRESETS``: every shipped preset builds the config it promises.
- ``EFFECTS``: a knob set on any entry path reaches Chrome on the next fetch,
  on tabs that already exist.
- ``MERGE``: a per-fetch setting overrides (headers) or adds to (blocks, scripts)
  the client's, and a ``Referer`` travels as the navigation referrer.
- ``PROXIES``: the proxy carries the page, authenticates, and moves at runtime.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import os
import re
import tempfile
import threading
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import onyxweb
import psutil
import pydantic
import pytest
from onyxweb import _LAUNCH_ONLY_FIELDS
from onyxweb.config import (
    _FLAT_KWARG_PATHS,
    _FORBIDDEN_HEADERS,
    _TOP_LEVEL_KWARGS,
    ChromeConfig,
    ClientConfig,
    EmulationConfig,
    FetchConfig,
    NetworkConfig,
    ScreenshotConfig,
    ScriptsConfig,
    UserAgentMetadata,
    ViewportConfig,
)
from onyxweb.presets import full, shell
from pytest_httpserver import HTTPServer
from werkzeug.datastructures import Headers

NEVER_FETCHED = "http://127.0.0.1:9/"  # validation stops these calls before Chrome
_NOT_CONFIG = ("ONYXWEB_PKG_DIR", "ONYXWEB_LOG")  # read by onyxweb itself, not ClientConfig


@pytest.fixture(scope="module", autouse=True)
def _clean_env() -> Iterator[None]:
    """Drop ``ONYXWEB_*`` config variables a developer shell may set, so defaults hold."""
    with pytest.MonkeyPatch.context() as mp:
        for name in list(os.environ):
            if name.startswith("ONYXWEB_") and name not in _NOT_CONFIG:
                mp.delenv(name)
        yield


@dataclass(frozen=True)
class Clients:
    """Open clients no row fetches with; each runtime entry path changes its own."""

    updated: onyxweb.Client  # update_config rows change this one
    assigned: onyxweb.Client  # setattr rows change this one
    aio: onyxweb.AsyncClient  # only refused changes reach this one


@pytest.fixture(scope="module")
def clients() -> Iterator[Clients]:
    made = Clients(
        onyxweb.Client(concurrency=1), onyxweb.Client(concurrency=1), onyxweb.AsyncClient()
    )
    try:
        yield made
    finally:
        made.updated.close()
        made.assigned.close()
        asyncio.run(made.aio.aclose())


def _at(data: Any, path: tuple[str, ...]) -> Any:
    """The value at a nested path; ``None`` below a parent that is itself ``None``."""
    for key in path:
        if data is None:
            return None
        data = data[key]
    return data


# --- flat kwargs on every entry path ----------------------------------------------

_META: dict[str, Any] = {
    "platform": "Windows",
    "platform_version": "",
    "architecture": "x86",
    "model": "",
    "mobile": False,
}
# Flat kwarg -> (nested path it lands at, a non-default value).
FLAT_KWARGS: dict[str, tuple[tuple[str, ...], Any]] = {
    "concurrency": (("concurrency",), 3),
    "wait_until": (("wait_until",), "domcontentloaded"),
    "wait_after_ms": (("wait_after_ms",), 7),
    "wait_after_post_load_ms": (("wait_after_post_load_ms",), 7),
    "bypass_anti_bot": (("bypass_anti_bot",), True),
    "capture_console_level": (("capture_console_level",), "all"),
    "hash_navigation": (("hash_navigation",), "continue"),
    "viewport": (("viewport", "width"), (640, 480)),
    "device_scale_factor": (("viewport", "device_scale_factor"), 2.0),
    "mobile": (("viewport", "mobile"), True),
    "user_agent": (("network", "user_agent"), "Table/1.0"),
    "user_agent_metadata": (("network", "user_agent_metadata", "platform"), _META),
    "proxy": (("network", "proxy"), "http://127.0.0.1:9"),
    "proxy_bypass_list": (("network", "proxy_bypass_list"), "<-loopback>"),
    "extra_headers": (("network", "extra_headers"), {"X-Table": "1"}),
    "ignore_https_errors": (("network", "ignore_https_errors"), True),
    "block_urls": (("network", "block_urls"), ["*://*/*.png"]),
    "disable_cache": (("network", "disable_cache"), True),
    "offline": (("network", "offline"), True),
    "latency_ms": (("network", "latency_ms"), 5.0),
    "download_bps": (("network", "download_bps"), 1000),
    "upload_bps": (("network", "upload_bps"), 1000),
    "locale": (("emulation", "locale"), "fr-FR"),
    "timezone": (("emulation", "timezone"), "Asia/Tokyo"),
    "geolocation": (("emulation", "geolocation"), (1.5, 2.5)),
    "prefers_color_scheme": (("emulation", "prefers_color_scheme"), "dark"),
    "javascript_enabled": (("emulation", "javascript_enabled"), False),
    "scripts": (("scripts", "on_load"), {"on_load": ["1"]}),
    "include_shadow_dom": (("include", "shadow_dom"), True),
    "include_iframes": (("include", "iframes"), True),
    "navigation_timeout_ms": (("timeout", "navigation_ms"), 1234),
    "launch_timeout_ms": (("timeout", "launch_ms"), 1234),
    "screenshot_timeout_ms": (("timeout", "screenshot_ms"), 1234),
    "queue_timeout_ms": (("timeout", "queue_ms"), 1234),
    "chrome_path": (("chrome", "path"), "/nonexistent/chrome"),
    "chrome_args": (("chrome", "args"), ["--table"]),
    "user_data_dir": (("chrome", "user_data_dir"), "/tmp/onyxweb-table"),
    "headless": (("chrome", "headless"), False),
    "engine": (("chrome", "engine"), "full"),
    "sandbox": (("chrome", "sandbox"), False),
}
# Fixed once Chrome is running: changing one at runtime must raise.
LAUNCH_ONLY_KWARGS = {
    "concurrency",
    "ignore_https_errors",
    "launch_timeout_ms",
    "chrome_path",
    "chrome_args",
    "user_data_dir",
    "headless",
    "engine",
    "sandbox",
}
BUILD_ENTRIES = ("from_flat", "env")
RUNTIME_ENTRIES = ("update_config", "setattr")


def _landed(value: Any, path: tuple[str, ...]) -> Any:
    """What a kwarg's value reads as at its nested path once validated."""
    if isinstance(value, tuple) and path[0] == "viewport":
        return value[0]
    if path[-1] == "platform":
        return value["platform"]
    if path[0] == "scripts":
        return value["on_load"]
    return value


def _field_path(kwarg: str) -> tuple[str, ...]:
    """The config field a flat kwarg sets: ``("network", "proxy")``, or itself at top level."""
    return _FLAT_KWARG_PATHS.get(kwarg, (kwarg,))


def _env_vars(kwarg: str, value: Any) -> dict[str, str]:
    """``ONYXWEB_*`` variables that set a flat kwarg; complex values are JSON, as pydantic reads."""
    if kwarg == "viewport":
        return {"ONYXWEB_VIEWPORT__WIDTH": str(value[0]), "ONYXWEB_VIEWPORT__HEIGHT": str(value[1])}
    name = "ONYXWEB_" + "__".join(_field_path(kwarg)).upper()
    if isinstance(value, bool):
        return {name: str(value).lower()}
    if isinstance(value, dict | list | tuple):
        return {name: json.dumps(value)}
    return {name: str(value)}


def _assign(client: onyxweb.Client | onyxweb.AsyncClient, kwarg: str, value: Any) -> None:
    """Set a flat kwarg by assignment through the live ``client.config`` view."""
    if kwarg == "viewport":
        client.config.viewport.width = value[0]
        client.config.viewport.height = value[1]
        return
    *parents, name = _field_path(kwarg)
    view = client.config
    for part in parents:
        view = getattr(view, part)
    setattr(view, name, value)


FLAT_CASES = [
    (kwarg, entry)
    for kwarg in FLAT_KWARGS
    for entry in (*BUILD_ENTRIES, *RUNTIME_ENTRIES)
    if not (kwarg in LAUNCH_ONLY_KWARGS and entry in RUNTIME_ENTRIES)
]


@pytest.mark.parametrize(("kwarg", "entry"), FLAT_CASES)
def test_flat_kwarg_lands_at_its_path(
    clients: Clients, monkeypatch: pytest.MonkeyPatch, kwarg: str, entry: str
) -> None:
    """Launch-only kwargs skip the runtime entries; ``LAUNCH_ONLY`` covers those."""
    path, value = FLAT_KWARGS[kwarg]
    expected = _landed(value, path)
    if entry == "from_flat":
        dump = ClientConfig.from_flat(**{kwarg: value}).model_dump()
    elif entry == "env":
        for name, text in _env_vars(kwarg, value).items():
            monkeypatch.setenv(name, text)
        dump = ClientConfig().model_dump()
    else:
        client = clients.updated if entry == "update_config" else clients.assigned
        assert _at(client.config.model_dump(), path) != expected, "the client already holds it"
        if entry == "update_config":
            client.update_config(**{kwarg: value})
        else:
            _assign(client, kwarg, value)
        dump = client.config.model_dump()
    assert _at(dump, path) == expected


def test_explicit_arguments_beat_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONYXWEB_CONCURRENCY", "99")
    monkeypatch.setenv("ONYXWEB_VIEWPORT__WIDTH", "2560")
    config = ClientConfig(concurrency=7)
    assert (config.concurrency, config.viewport.width) == (7, 2560)
    # A flat kwarg sets its own field; the environment still fills the rest of its section.
    monkeypatch.setenv("ONYXWEB_CHROME__SANDBOX", "false")
    chrome = ClientConfig.from_flat(engine="full").chrome
    assert (chrome.engine, chrome.sandbox) == ("full", False)
    assert ClientConfig.from_flat(engine="full", sandbox=True).chrome.sandbox is True


def test_every_flat_kwarg_round_trips(clients: Clients) -> None:
    """A config holding every flat kwarg survives a dump and a JSON trip, through the view too."""
    config = ClientConfig.from_flat(**{k: v for k, (_, v) in FLAT_KWARGS.items()})
    assert isinstance(config.network.user_agent_metadata, UserAgentMetadata)
    assert isinstance(config.scripts, ScriptsConfig)
    assert ClientConfig.model_validate(config.model_dump()) == config
    assert ClientConfig.model_validate_json(config.model_dump_json()) == config
    view = clients.updated.config
    assert (
        ClientConfig.model_validate_json(view.model_dump_json()).model_dump() == view.model_dump()
    )


def test_snapshot_is_detached(clients: Clients) -> None:
    whole = clients.assigned.config.snapshot()
    network = clients.assigned.config.network.snapshot()
    assert isinstance(whole, ClientConfig) and isinstance(network, NetworkConfig)
    whole.network.user_agent = network.user_agent = "SNAPSHOT_EDIT"
    assert clients.assigned.config.network.user_agent != "SNAPSHOT_EDIT"


# --- defaults ---------------------------------------------------------------------

_CLIENT_DEFAULTS: dict[str, Any] = {
    "concurrency": 16,
    "wait_until": "load",
    "wait_after_ms": 0,
    "wait_after_post_load_ms": 0,
    "bypass_anti_bot": False,
    "capture_console_level": "error",
    "hash_navigation": "reload",
    "viewport": {"width": 1200, "height": 800, "device_scale_factor": 1.0, "mobile": False},
    "network": {
        "user_agent": None,
        "user_agent_metadata": None,
        "proxy": None,
        "proxy_bypass_list": None,
        "extra_headers": {},
        "ignore_https_errors": False,
        "block_urls": [],
        "disable_cache": False,
        "offline": False,
        "latency_ms": None,
        "download_bps": None,
        "upload_bps": None,
    },
    "emulation": {
        "locale": None,
        "timezone": None,
        "geolocation": None,
        "prefers_color_scheme": None,
        "javascript_enabled": True,
    },
    "scripts": {
        "on_new_document": [],
        "on_dom_content_loaded": [],
        "on_load": [],
        "isolated_world": [],
        "isolated_world_name": "util",  # generic, not a branded world name
        "url_scoped": {},
    },
    "include": {"shadow_dom": False, "iframes": False},
    "timeout": {
        "navigation_ms": 30_000,
        "launch_ms": 15_000,
        "screenshot_ms": 5_000,
        "queue_ms": None,  # off unless set: a call waits for a free tab as long as it takes
    },
    "chrome": {
        "path": None,
        "args": [],
        "user_data_dir": None,
        "headless": True,
        "engine": "shell",
        "sandbox": True,  # on unless a container, root or BBOT asks for it off
    },
}
_WAITS: dict[str, Any] = {
    "wait_until": None,
    "wait_after_ms": None,
    "wait_after_post_load_ms": None,
}
# Model -> (build with nothing set, its whole dump). A new field must pick its default here.
DEFAULTS: dict[str, tuple[Callable[[], pydantic.BaseModel], dict[str, Any]]] = {
    "ClientConfig": (ClientConfig, _CLIENT_DEFAULTS),
    "FetchConfig": (
        FetchConfig,
        {
            "extra_headers": {},
            "scripts": [],
            "post_load_scripts": [],
            "block_urls": [],
            "actions": [],
            "block_navigation": False,
            "bypass_anti_bot": None,
            "hash_navigation": None,
            "timeout_ms": None,
            **_WAITS,
        },
    ),
    "ScreenshotConfig": (
        ScreenshotConfig,
        {
            "viewport": None,
            "full_page": False,
            "timeout_ms": None,
            "extra_headers": {},
            "format": "png",
            "quality": None,
            **_WAITS,
        },
    ),
    "UserAgentMetadata": (
        lambda: UserAgentMetadata(**_META),
        {
            **_META,
            "brands": None,
            "full_version_list": None,
            "bitness": None,
            "wow64": False,
            "form_factors": None,
        },
    ),
}


@pytest.mark.parametrize("model", list(DEFAULTS))
def test_defaults(model: str) -> None:
    build, expected = DEFAULTS[model]
    assert build().model_dump() == expected


# --- invalid and accepted input -----------------------------------------------------

Invalid = tuple[Callable[[Clients], object], type[Exception], tuple[str, ...]]


def _load_snapshot(text: str) -> object:
    """Load a snapshot file holding `text`."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text(text)
        return onyxweb.RenderResult.load(path)


_EXTRA = "Extra inputs are not permitted"
_NO_SCHEME = "has no scheme"
INVALID: dict[str, Invalid] = {
    "snapshot_not_json": (
        lambda c: _load_snapshot("not json at all"),
        ValueError,
        ("not an onyxweb snapshot", "RenderResult.save"),
    ),
    "snapshot_without_the_marker": (
        lambda c: _load_snapshot('{"html": "<p>x</p>"}'),
        ValueError,
        ("not an onyxweb snapshot", "RenderResult.save"),
    ),
    "snapshot_from_a_newer_version": (
        lambda c: _load_snapshot('{"onyxweb_snapshot": 99}'),
        ValueError,
        ("version 99", "RenderResult.save"),
    ),
    "viewport_zero": (
        lambda c: ViewportConfig(width=0),
        pydantic.ValidationError,
        ("greater than or equal to 1",),
    ),
    "viewport_negative": (
        lambda c: ViewportConfig(width=-1),
        pydantic.ValidationError,
        ("greater than or equal to 1",),
    ),
    "network_unknown_field": (
        lambda c: NetworkConfig(usr_agent="oops"),  # type: ignore[call-arg]
        pydantic.ValidationError,
        ("usr_agent", _EXTRA),
    ),
    "scripts_unknown_field": (
        lambda c: ScriptsConfig(on_navigation=["x"]),  # type: ignore[call-arg]
        pydantic.ValidationError,
        ("on_navigation", _EXTRA),
    ),
    "ua_metadata_unknown_field": (
        lambda c: UserAgentMetadata(**_META, nonsense="x"),  # type: ignore[call-arg]
        pydantic.ValidationError,
        ("nonsense", _EXTRA),
    ),
    "ua_metadata_missing_fields": (
        lambda c: UserAgentMetadata(),  # type: ignore[call-arg]
        pydantic.ValidationError,
        ("platform", "Field required"),
    ),
    "color_scheme_unknown": (
        lambda c: EmulationConfig(prefers_color_scheme="sepia"),  # type: ignore[arg-type]
        pydantic.ValidationError,
        ("'light' or 'dark'",),
    ),
    "engine_unknown": (
        lambda c: ChromeConfig(engine="turbo"),  # type: ignore[arg-type]
        pydantic.ValidationError,
        ("'full' or 'shell'",),
    ),
    "console_level_unknown": (
        lambda c: ClientConfig(capture_console_level="invalid"),  # type: ignore[arg-type]
        pydantic.ValidationError,
        ("'all', 'warn' or 'error'",),
    ),
    "hash_navigation_unknown": (
        lambda c: FetchConfig(hash_navigation="sideways"),  # type: ignore[arg-type]
        pydantic.ValidationError,
        ("'reload' or 'continue'",),
    ),
    "queue_timeout_zero": (
        lambda c: ClientConfig.from_flat(queue_timeout_ms=0),
        pydantic.ValidationError,
        ("greater than or equal to 1",),
    ),
    "screenshot_format_unknown": (
        lambda c: ScreenshotConfig(format="tiff"),  # type: ignore[arg-type]
        pydantic.ValidationError,
        ("'png', 'jpeg' or 'webp'",),
    ),
    "screenshot_quality_over_100": (
        lambda c: ScreenshotConfig(format="jpeg", quality=150),
        pydantic.ValidationError,
        ("less than or equal to 100",),
    ),
    "screenshot_kwarg_checked_before_navigating": (
        lambda c: c.updated.screenshot(NEVER_FETCHED, format="tiff"),
        pydantic.ValidationError,
        ("'png', 'jpeg' or 'webp'",),
    ),
    "client_positional_argument": (
        lambda c: onyxweb.Client("x"),
        TypeError,
        ("Client() takes only keyword args", "config=ClientConfig"),
    ),
    "async_client_positional_argument": (
        lambda c: onyxweb.AsyncClient("x"),
        TypeError,
        ("AsyncClient() takes only keyword args", "config=ClientConfig"),
    ),
    "client_config_and_kwargs": (
        lambda c: onyxweb.Client(config=ClientConfig(), user_agent="x"),
        TypeError,
        ("config=... or flat kwargs, not both",),
    ),
    "async_client_config_and_kwargs": (
        lambda c: onyxweb.AsyncClient(config=ClientConfig(), user_agent="x"),
        TypeError,
        ("config=... or flat kwargs, not both",),
    ),
    "update_config_positional_argument": (
        lambda c: c.updated.update_config("x"),
        TypeError,
        ("update_config() takes only keyword args",),
    ),
    "update_config_config_and_kwargs": (
        lambda c: c.updated.update_config(config=ClientConfig(), user_agent="x"),
        TypeError,
        ("config= OR flat kwargs, not both",),
    ),
    "from_flat_unknown_kwarg": (
        lambda c: ClientConfig.from_flat(nonsense_key=1),
        TypeError,
        ("unknown ClientConfig kwarg: 'nonsense_key'", "use one of", "user_agent"),
    ),
    "update_config_unknown_kwarg": (
        lambda c: c.updated.update_config(nonsense_key=1),
        TypeError,
        ("unknown ClientConfig kwarg: 'nonsense_key'", "use one of", "user_agent"),
    ),
    "from_flat_viewport_not_a_pair": (
        lambda c: ClientConfig.from_flat(viewport=5),
        TypeError,
        ("viewport must be", "ViewportConfig", "got int"),
    ),
    "update_config_viewport_not_a_pair": (
        lambda c: c.updated.update_config(viewport=5),
        TypeError,
        ("viewport must be", "ViewportConfig", "got int"),
    ),
    "update_config_scripts_not_a_dict": (
        lambda c: c.updated.update_config(scripts=5),
        TypeError,
        ("scripts must be dict or ScriptsConfig", "got int"),
    ),
    "fetch_unknown_kwarg": (
        lambda c: c.updated.fetch(NEVER_FETCHED, nonsense=1),
        TypeError,
        ("unknown fetch kwarg: 'nonsense'", "use one of", "timeout_ms"),
    ),
    "screenshot_unknown_kwarg": (
        lambda c: c.updated.screenshot(NEVER_FETCHED, nonsense=1),
        TypeError,
        ("unknown screenshot kwarg: 'nonsense'", "use one of", "full_page"),
    ),
    # Chrome parses each block_urls entry as a URLPattern, which needs a scheme; a bare
    # glob made every fetch fail with a raw CDP error.
    "block_url_without_scheme_in_network_config": (
        lambda c: NetworkConfig(block_urls=["*doubleclick*"]),
        pydantic.ValidationError,
        ("'*doubleclick*'", _NO_SCHEME, "*://*.doubleclick.net/*"),
    ),
    "block_url_without_scheme_in_fetch_config": (
        lambda c: FetchConfig(block_urls=["*/x.png"]),
        pydantic.ValidationError,
        ("'*/x.png'", _NO_SCHEME, "*://*.doubleclick.net/*"),
    ),
    "block_url_without_scheme_via_from_flat": (
        lambda c: ClientConfig.from_flat(block_urls=["example.com/*"]),
        pydantic.ValidationError,
        ("'example.com/*'", _NO_SCHEME),
    ),
    "block_url_without_scheme_via_update_config": (
        lambda c: c.updated.update_config(block_urls=["*.png"]),
        pydantic.ValidationError,
        ("'*.png'", _NO_SCHEME),
    ),
    "block_url_without_scheme_via_fetch_kwarg": (
        lambda c: c.updated.fetch(NEVER_FETCHED, block_urls=["/x.png"]),
        pydantic.ValidationError,
        ("'/x.png'", _NO_SCHEME),
    ),
}

FORBIDDEN = (
    "Cookie",
    "Cookie2",
    "Set-Cookie",
    "Host",
    "Origin",
    "Content-Length",
    "Transfer-Encoding",
    "Connection",
)
# Every place extra_headers enters; a per-call kwarg sends the name lowercased.
_HEADER_ENTRIES: dict[str, Callable[[Clients, dict[str, str]], object]] = {
    "fetch_config": lambda c, h: FetchConfig(extra_headers=h),
    "screenshot_config": lambda c, h: ScreenshotConfig(extra_headers=h),
    "from_flat": lambda c, h: ClientConfig.from_flat(extra_headers=h),
    "update_config": lambda c, h: c.updated.update_config(extra_headers=h),
    "fetch_kwarg": lambda c, h: c.updated.fetch(NEVER_FETCHED, extra_headers=h),
}


def _send_header(
    entry: Callable[[Clients, dict[str, str]], object], header: str, c: Clients
) -> object:
    return entry(c, {header: "v"})


for _header in FORBIDDEN:
    for _entry, _send in _HEADER_ENTRIES.items():
        _sent = _header.lower() if _entry == "fetch_kwarg" else _header
        INVALID[f"forbidden_header_{_header.lower()}_via_{_entry}"] = (
            partial(_send_header, _send, _sent),
            pydantic.ValidationError,
            (f"cannot set '{_sent}'",),
        )


@pytest.mark.parametrize("name", list(INVALID))
def test_invalid_input(clients: Clients, name: str) -> None:
    call, error, says = INVALID[name]
    with pytest.raises(error) as exc:
        call(clients)
    for fragment in says:
        assert fragment in str(exc.value), str(exc.value)


_VALID_PATTERNS = [
    "*://*.doubleclick.net/*",
    "*://*:*/blocked.png",
    "https://example.com/*",
    "http://127.0.0.1:*/x",
    "HTTP://X/*",
    "data:*",
    "*:*",
]
# Input beside an INVALID rule -> (build, the value it must hold afterwards).
ACCEPTED: dict[str, tuple[Callable[[], object], object]] = {
    # Referer travels as the navigation referrer instead, so it isn't forbidden.
    "referer_header": (
        lambda: FetchConfig(extra_headers={"Referer": "http://foo.bar/"}).extra_headers,
        {"Referer": "http://foo.bar/"},
    ),
    "ordinary_headers": (
        lambda: (
            FetchConfig(
                extra_headers={"X-Custom": "ok", "User-Agent": "Bot/1.0", "DNT": "1"}
            ).extra_headers
        ),
        {"X-Custom": "ok", "User-Agent": "Bot/1.0", "DNT": "1"},
    ),
    "url_patterns_with_a_scheme_in_network_config": (
        lambda: NetworkConfig(block_urls=_VALID_PATTERNS).block_urls,
        _VALID_PATTERNS,
    ),
    "url_patterns_with_a_scheme_in_fetch_config": (
        lambda: FetchConfig(block_urls=_VALID_PATTERNS).block_urls,
        _VALID_PATTERNS,
    ),
    "blank_block_urls_are_dropped": (
        lambda: NetworkConfig(block_urls=["", "  ", "*://a/*"]).block_urls,
        ["*://a/*"],
    ),
}


@pytest.mark.parametrize("name", list(ACCEPTED))
def test_accepted_input(name: str) -> None:
    build, expected = ACCEPTED[name]
    assert build() == expected


# --- launch-only fields -----------------------------------------------------------------


@pytest.mark.parametrize("entry", ["update_config", "update_config_object", "setattr"])
@pytest.mark.parametrize("kind", ["Client", "AsyncClient"])
@pytest.mark.parametrize("kwarg", sorted(LAUNCH_ONLY_KWARGS))
def test_launch_only_field_refuses_a_runtime_change(
    clients: Clients, kwarg: str, kind: str, entry: str
) -> None:
    client = clients.updated if kind == "Client" else clients.aio
    _, value = FLAT_KWARGS[kwarg]
    *parents, name = _field_path(kwarg)
    before = client.config.model_dump()
    dotted = re.escape(".".join(_field_path(kwarg)))
    with pytest.raises(ValueError, match=f"launch-only field '{dotted}'.*Create a new {kind} "):
        if entry == "update_config":
            client.update_config(**{kwarg: value})
        elif entry == "update_config_object":
            changed = client.config.snapshot()
            setattr(_at_model(changed, parents), name, value)
            client.update_config(config=changed)
        else:
            _assign(client, kwarg, value)
    assert client.config.model_dump() == before


def _at_model(model: Any, path: list[str]) -> Any:
    for part in path:
        model = getattr(model, part)
    return model


# --- presets ----------------------------------------------------------------------------

_OVERRIDE = "shell.stealth.BASIC with user_agent pre-merged"
# Preset -> (the flat kwargs, path -> value its config must hold).
PRESETS: dict[str, tuple[dict[str, Any], dict[tuple[str, ...], Any]]] = {
    "shell.stealth.BASIC": (
        shell.stealth.BASIC,
        {
            ("chrome", "engine"): "shell",
            ("bypass_anti_bot",): True,
            ("network", "user_agent"): shell.stealth.BASIC_UA,
            ("network", "user_agent_metadata", "platform"): "Linux",
            ("scripts", "on_new_document"): shell.stealth.BASIC_PATCHES,
        },
    ),
    "shell.stealth.FINGERPRINT": (
        shell.stealth.FINGERPRINT,
        {
            ("chrome", "engine"): "shell",
            ("network", "user_agent"): shell.stealth.BASIC_UA,
            ("scripts", "on_new_document"): shell.stealth.FINGERPRINT_PATCHES,
        },
    ),
    "shell.recon.FAST": (
        shell.recon.FAST,
        {
            ("chrome", "engine"): "shell",
            ("bypass_anti_bot",): True,
            ("emulation", "javascript_enabled"): False,
            ("timeout", "navigation_ms"): 5_000,
            ("network", "block_urls"): shell.recon.FAST["block_urls"],
        },
    ),
    "shell.archival.FULL_PAGE": (
        shell.archival.FULL_PAGE,
        {
            ("chrome", "engine"): "shell",
            ("viewport", "width"): 1920,
            ("viewport", "height"): 1080,
            ("wait_after_ms",): 2_000,
        },
    ),
    # Real Chrome is the stealth, so the full preset ships no patches and no UA override.
    "full.stealth.BASIC": (
        full.stealth.BASIC,
        {
            ("chrome", "engine"): "full",
            ("bypass_anti_bot",): True,
            ("network", "user_agent"): None,
            ("scripts", "on_new_document"): [],
        },
    ),
    # Python forbids a key twice across ** spreads, so a caller merges the dict first.
    _OVERRIDE: (
        {**shell.stealth.BASIC, "user_agent": "MyBot/9.9"},
        {
            ("network", "user_agent"): "MyBot/9.9",
            ("scripts", "on_new_document"): shell.stealth.BASIC_PATCHES,
        },
    ),
}


def _shipped_presets() -> set[str]:
    """Every ``engine.purpose.NAME`` bundle under ``onyxweb.presets``, as the CLI lists them."""
    names: set[str] = set()
    for engine in (full, shell):
        for purpose in engine.__all__:
            module = getattr(engine, purpose)
            for attr in dir(module):
                value = getattr(module, attr)
                # A preset pins its engine; that skips parts like BASIC_UA_METADATA.
                if attr.isupper() and isinstance(value, dict) and "engine" in value:
                    names.add(f"{engine.__name__.rsplit('.', 1)[-1]}.{purpose}.{attr}")
    return names


@pytest.mark.parametrize("name", list(PRESETS))
def test_preset_builds_its_config(name: str) -> None:
    kwargs, expected = PRESETS[name]
    dump = ClientConfig.from_flat(**kwargs).model_dump()
    for path, value in expected.items():
        assert _at(dump, path) == value, path


def test_tables_match_the_code() -> None:
    """The tables above are only a spec while they list what the code holds.

    No row can check its own table is complete, so this guards each against drift.
    """
    assert set(FLAT_KWARGS) == {"viewport", "scripts", *_TOP_LEVEL_KWARGS, *_FLAT_KWARG_PATHS}
    assert {_field_path(k) for k in LAUNCH_ONLY_KWARGS} == set(_LAUNCH_ONLY_FIELDS)
    assert {h.lower() for h in FORBIDDEN} == set(_FORBIDDEN_HEADERS)
    assert set(PRESETS) - {_OVERRIDE} == _shipped_presets()


# --- effects on the next fetch ------------------------------------------------------------

# A page that shows every knob below: an image to block, a console.log, and an open
# shadow root whose marker is assembled at runtime so the source can't match.
_PROBE_PAGE = (
    "<html><body><div id='host'></div><img src='/blocked.png'><script>"
    "console.log('PROBE_LOG');"
    "document.getElementById('host').attachShadow({mode:'open'})"
    ".innerHTML='<b>'+'SHADOW_'+'PROBE</b>';"
    "</script></body></html>"
)
# Read back in-page after load, one value per knob that has no wire signal.
_PROBE_JS = (
    "({locale: Intl.DateTimeFormat().resolvedOptions().locale,"
    " nav_language: navigator.language,"
    " timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,"
    " width: innerWidth,"
    " scheme: [matchMedia('(prefers-color-scheme: dark)').matches,"
    " matchMedia('(prefers-color-scheme: light)').matches],"
    " scripts: {new_document: window.__newdoc || null, dom_content_loaded: window.__dcl || null,"
    " load: window.__load || null, isolated_dom: document.documentElement.dataset.iso || null,"
    " isolated_global: window.__iso || null, url_scoped_hit: window.__hit || null,"
    " url_scoped_miss: window.__miss || null}})"
)
_PROBE_META = {
    "brands": [{"brand": "ProbeBrand", "version": "99"}],
    "platform": "Windows",
    "platform_version": "10.0.0",
    "architecture": "x86",
    "model": "",
    "mobile": True,
}
_PROBE_SCRIPTS = {
    "on_new_document": ["window.__newdoc = 'ran'"],
    "on_dom_content_loaded": ["window.__dcl = 'ran'"],
    "on_load": ["window.__load = 'ran'"],
    # Runs before <html> exists, so it marks the shared DOM once the document is parsed.
    "isolated_world": [
        "window.__iso = 'ran';"
        "document.addEventListener('DOMContentLoaded',"
        " () => { document.documentElement.dataset.iso = 'ran'; })"
    ],
    "url_scoped": {"/probe": ["window.__hit = 'ran'"], "/elsewhere": ["window.__miss = 'ran'"]},
}


@dataclass(frozen=True)
class Probe:
    """What one fetch showed: the page, in-page readings, and the requests it made."""

    html: str
    js: dict[str, Any]
    console: list[str]
    wire: Headers  # headers of the page request
    hits: Counter[str]  # requests per path during the fetch


def _read(r: onyxweb.RenderResult, server: HTTPServer, seen: int, path: str) -> Probe:
    requests = [req for req, _ in server.log[seen:]]
    page = next(req for req in requests if req.path == path)
    return Probe(
        html=r.html,
        js=r.post_load_results[0],
        console=[m.text for m in r.console_messages],
        wire=page.headers,
        hits=Counter(req.path for req in requests),
    )


# Knob -> (value, reading off a Probe, expected reading). All knobs apply together.
EFFECTS: dict[str, tuple[Any, Callable[[Probe], Any], Any]] = {
    "user_agent": ("ProbeUA/1.0", lambda p: p.wire.get("User-Agent"), "ProbeUA/1.0"),
    "user_agent_metadata": (
        _PROBE_META,
        lambda p: (
            p.wire.get("Sec-Ch-Ua-Platform"),
            p.wire.get("Sec-Ch-Ua-Mobile"),
            "ProbeBrand" in (p.wire.get("Sec-Ch-Ua") or ""),
        ),
        ('"Windows"', "?1", True),
    ),
    "extra_headers": ({"X-Probe": "on"}, lambda p: p.wire.get("X-Probe"), "on"),
    "block_urls": (["*://*:*/blocked.png"], lambda p: p.hits["/blocked.png"], 0),
    # Intl, navigator.language and the wire header must all agree with the knob.
    "locale": (
        "fr-FR",
        lambda p: (p.js["locale"], p.js["nav_language"], p.wire.get("Accept-Language")),
        ("fr-FR", "fr-FR", "fr-FR,fr;q=0.9"),
    ),
    "timezone": ("Asia/Tokyo", lambda p: p.js["timezone"], "Asia/Tokyo"),
    "viewport": ((640, 480), lambda p: p.js["width"], 640),
    "prefers_color_scheme": ("dark", lambda p: p.js["scheme"], [True, False]),
    "scripts": (
        _PROBE_SCRIPTS,
        lambda p: p.js["scripts"],
        {
            "new_document": "ran",
            "dom_content_loaded": "ran",
            "load": "ran",
            "isolated_dom": "ran",
            "isolated_global": None,  # the main world can't see the isolated one
            "url_scoped_hit": "ran",
            "url_scoped_miss": None,
        },
    ),
    "capture_console_level": ("all", lambda p: "PROBE_LOG" in p.console, True),
    "include_shadow_dom": (True, lambda p: "SHADOW_PROBE" in p.html, True),
}
EFFECT_ENTRIES = (
    "constructor",
    "constructor_config",
    "env",
    "update_config",
    "update_config_object",
    "setattr",
    "async_update_config",
)


@pytest.fixture(scope="module")
def observed() -> Iterator[dict[str, Probe]]:
    """One probe per entry path, plus a baseline from an untouched client.

    Each runtime entry fetches before the change, so the change must reach a tab
    that already exists rather than one created after it.
    """
    knobs = {name: value for name, (value, _, _) in EFFECTS.items()}
    whole = ClientConfig.from_flat(concurrency=1, **knobs)
    probes: dict[str, Probe] = {}
    with HTTPServer() as server:
        server.expect_request("/probe").respond_with_data(_PROBE_PAGE, content_type="text/html")
        server.expect_request("/blocked.png").respond_with_data(b"", content_type="image/png")

        def probe(client: onyxweb.Client) -> Probe:
            seen = len(server.log)
            r = client.fetch(server.url_for("/probe"), post_load_scripts=[_PROBE_JS])
            return _read(r, server, seen, "/probe")

        def changed(apply: Callable[[onyxweb.Client], None]) -> Probe:
            with onyxweb.Client(concurrency=1) as client:
                before = probe(client)
                apply(client)
                probes.setdefault("baseline", before)
                return probe(client)

        probes["update_config"] = changed(lambda c: c.update_config(**knobs))
        probes["update_config_object"] = changed(lambda c: c.update_config(config=whole))

        def assign_each(client: onyxweb.Client) -> None:
            for kwarg, value in knobs.items():
                _assign(client, kwarg, value)

        probes["setattr"] = changed(assign_each)
        with onyxweb.Client(concurrency=1, **knobs) as client:
            probes["constructor"] = probe(client)
        with onyxweb.Client(config=whole) as client:
            probes["constructor_config"] = probe(client)
        with pytest.MonkeyPatch.context() as mp:
            for kwarg, value in knobs.items():
                for name, text in _env_vars(kwarg, value).items():
                    mp.setenv(name, text)
            with onyxweb.Client(concurrency=1) as client:
                probes["env"] = probe(client)

        async def async_changed() -> Probe:
            async with onyxweb.AsyncClient(concurrency=1) as ac:
                await ac.fetch(server.url_for("/probe"))
                ac.update_config(**knobs)
                seen = len(server.log)
                r = await ac.fetch(server.url_for("/probe"), post_load_scripts=[_PROBE_JS])
                return _read(r, server, seen, "/probe")

        probes["async_update_config"] = asyncio.run(async_changed())
        yield probes


@pytest.mark.parametrize("entry", EFFECT_ENTRIES)
@pytest.mark.parametrize("knob", list(EFFECTS))
def test_knob_takes_effect_on_the_next_fetch(
    observed: dict[str, Probe], knob: str, entry: str
) -> None:
    _, read, expected = EFFECTS[knob]
    assert read(observed["baseline"]) != expected, "the default already shows it"
    assert read(observed[entry]) == expected


def test_explicit_profile_dir_is_used(tmp_path: Path) -> None:
    """``user_data_dir`` replaces the per-launch temporary profile, so Chrome writes there.

    A launch-only knob can't go through ``EFFECTS``, which changes a running client.
    """
    profile = tmp_path / "profile"
    with onyxweb.Client(concurrency=1, user_data_dir=str(profile)) as client:
        assert client.fetch("data:text/html,<p>x</p>").status_code == 200
    assert profile.is_dir() and any(profile.iterdir())


@pytest.mark.parametrize("engine", ["shell", "full"])
@pytest.mark.parametrize("sandbox", [True, False])
@pytest.mark.parametrize(
    "args", [[], ["disable-features=Translate"]], ids=["no_extra_args", "own_disable_features"]
)
def test_launch_flags_reach_chrome(engine: str, sandbox: bool, args: list[str]) -> None:
    """The flags a launch asks for are on Chrome's command line, on either engine.

    ``sandbox=False`` alone puts ``--no-sandbox`` there. The full engine also turns the
    back/forward cache off, even beside a caller's own ``disable-features``: Chrome keeps only
    the last such flag, and each cached page holds a renderer process, so one tab's memory grew
    about 14 MB with every fetch (11 to 15 renderers after 60 fetches, 5 to 8 without it).

    New test: a launch-only knob can't go through ``EFFECTS``, which changes a running client.
    The shell engine's own copy of ``--no-sandbox`` once reached Chrome as ``----no-sandbox``,
    which Chrome ignores, so its sandbox could not be switched off.
    """
    before = {p.pid for p in psutil.Process().children()}
    try:
        client = onyxweb.Client(concurrency=1, engine=engine, sandbox=sandbox, chrome_args=args)
    except onyxweb.OnyxwebError as e:
        if "not found" in str(e).lower():
            pytest.skip(f"{engine} Chrome unavailable: {e}")
        raise
    with client:
        launched = [
            p
            for p in psutil.Process().children()
            if p.pid not in before and any(n in p.name().lower() for n in ("chrome", "wrapper"))
        ]
        assert launched, "expected this client to start a Chrome process"
        # Off Linux the wrapper supervises Chrome, so it is the child, with the same flags.
        flags = min(launched, key=lambda p: p.pid).cmdline()
    assert ("--no-sandbox" in flags) == (not sandbox), flags
    if engine == "full":
        disabled = [f for f in flags if f.startswith("--disable-features=")]
        assert len(disabled) == 1, f"Chrome keeps only the last --disable-features: {disabled}"
        assert "BackForwardCache" in disabled[0], disabled
        assert ("Translate" in disabled[0]) == bool(args), disabled


def test_launch_timeout_ms_bounds_the_browser_launch(tmp_path: Path) -> None:
    """``launch_timeout_ms`` must actually bound the launch. A stub ``chrome_path``
    that never emits Chrome's DevTools startup line hangs ``Browser::launch``
    forever — only the timeout can end it, so this can't pass by accident."""
    stub = tmp_path / "never-launches.sh"
    stub.write_text("#!/bin/sh\nsleep 2\n")
    stub.chmod(0o755)
    with pytest.raises(TimeoutError):
        onyxweb.Client(chrome_path=str(stub), launch_timeout_ms=500)


def test_screenshot_timeout_ms_bounds_a_plain_screenshot_call() -> None:
    """A plain ``screenshot()`` with no per-call override falls back to
    ``screenshot_timeout_ms``, not ``navigation_timeout_ms`` — a settle well
    under the (generous) nav budget but over the (tight) screenshot one must
    still time out."""
    with (
        onyxweb.Client(concurrency=1, navigation_timeout_ms=10_000, screenshot_timeout_ms=300) as c,
        pytest.raises(TimeoutError),
    ):
        c.screenshot("data:text/html,<html></html>", wait_after_ms=600)


def test_user_agent_metadata_alone_still_reaches_the_wire(httpserver: HTTPServer) -> None:
    """``user_agent_metadata`` must take effect even without an explicit
    ``user_agent`` — the UA-override CDP call used to skip entirely unless
    ``user_agent`` or ``locale`` was also set."""
    httpserver.expect_request("/").respond_with_data(
        "<html><body>x</body></html>", content_type="text/html"
    )
    meta = {**_META, "brands": [{"brand": "OnlyMetaBrand", "version": "77"}]}
    with onyxweb.Client(concurrency=1, user_agent_metadata=meta) as c:
        c.fetch(httpserver.url_for("/"))
    wire = httpserver.log[0][0].headers.get("Sec-CH-UA") or ""
    assert "OnlyMetaBrand" in wire, f"metadata never reached the wire: {wire!r}"


# --- per-fetch settings merge with the client's --------------------------------------

_MERGE_PAGE = (
    "<html><body><img src='/beacon/base.png'><img src='/beacon/call.png'>"
    "<img src='/beacon/free.png'>"
    "<script>window.__order = (window.__order || []).concat('page');</script></body></html>"
)
_MERGE_JS = "({order: window.__order || null, referrer: document.referrer})"


def _push(label: str) -> str:
    return f"window.__order = (window.__order || []).concat('{label}');"


@dataclass(frozen=True)
class Merge:
    """Client kwargs, fetch kwargs, and what the page saw. ``{origin}`` is the server."""

    client: dict[str, Any]
    fetch: dict[str, Any]
    read: Callable[[Probe], object]
    expected: object


def _referer(p: Probe) -> object:
    """The Referer the server saw, and ``document.referrer`` in the page."""
    return p.wire.get("Referer"), p.js["referrer"]


MERGE: dict[str, Merge] = {
    "headers_per_call_override_the_client": Merge(
        {"extra_headers": {"X-Base": "base", "X-Both": "base"}},
        {"extra_headers": {"X-Both": "call", "X-Call": "call"}},
        lambda p: [p.wire.get(h) for h in ("X-Base", "X-Both", "X-Call")],
        ["base", "call", "call"],
    ),
    "block_urls_per_call_add_to_the_client": Merge(
        {"block_urls": ["*://*:*/beacon/base.png"]},
        {"block_urls": ["*://*:*/beacon/call.png"]},
        lambda p: [p.hits[f"/beacon/{b}.png"] for b in ("base", "call", "free")],
        [0, 0, 1],
    ),
    "scripts_per_call_run_once_after_the_client_before_the_page": Merge(
        {"scripts": {"on_new_document": [_push("base")]}},
        {"scripts": [_push("call1"), _push("call2")]},
        lambda p: p.js["order"],
        ["base", "call1", "call2", "page"],
    ),
    # Chrome refuses a cross-origin Referer set as a header, so it goes to Page.navigate.
    "referer_cross_origin": Merge(
        {},
        {"extra_headers": {"Referer": "http://foo.bar/X"}},
        _referer,
        ("http://foo.bar/X", "http://foo.bar/X"),
    ),
    "referer_same_origin": Merge(
        {},
        {"extra_headers": {"Referer": "{origin}/sibling"}},
        _referer,
        ("{origin}/sibling", "{origin}/sibling"),
    ),
    "referer_lowercase_key": Merge(
        {},
        {"extra_headers": {"referer": "http://foo.bar/lc"}},
        _referer,
        ("http://foo.bar/lc", "http://foo.bar/lc"),
    ),
    "referer_from_the_client": Merge(
        {"extra_headers": {"Referer": "http://foo.bar/base"}},
        {},
        _referer,
        ("http://foo.bar/base", "http://foo.bar/base"),
    ),
    # The DOMContentLoaded race must keep the navigate referrer.
    "referer_with_domcontentloaded": Merge(
        {},
        {"extra_headers": {"Referer": "http://ref.example/x"}, "wait_until": "domcontentloaded"},
        _referer,
        ("http://ref.example/x", "http://ref.example/x"),
    ),
}


def _fill(value: Any, origin: str) -> Any:
    """Replace ``{origin}`` inside strings, lists, tuples and dicts."""
    if isinstance(value, str):
        return value.replace("{origin}", origin)
    if isinstance(value, dict):
        return {k: _fill(v, origin) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_fill(v, origin) for v in value)
    return value


@pytest.mark.parametrize("name", list(MERGE))
def test_per_fetch_setting_merges_with_the_client(httpserver: HTTPServer, name: str) -> None:
    row = MERGE[name]
    httpserver.expect_request("/merge").respond_with_data(_MERGE_PAGE, content_type="text/html")
    for beacon in ("base", "call", "free"):
        httpserver.expect_request(f"/beacon/{beacon}.png").respond_with_data(b"")
    origin = httpserver.url_for("/").rstrip("/")
    with onyxweb.Client(concurrency=1, **row.client) as client:
        r = client.fetch(
            httpserver.url_for("/merge"), post_load_scripts=[_MERGE_JS], **_fill(row.fetch, origin)
        )
    assert row.read(_read(r, httpserver, 0, "/merge")) == _fill(row.expected, origin)


# --- proxies ------------------------------------------------------------------------------

_BYPASS = "<-loopback>"  # drop the implicit loopback bypass, so localhost goes through the proxy


@dataclass
class ProxyState:
    """What one local forward proxy demanded and relayed."""

    credentials: tuple[str, str] | None  # the Basic credentials it demands, if any
    forwarded: list[str] = field(default_factory=list)  # absolute URIs it relayed
    lock: threading.Lock = field(default_factory=threading.Lock)


def _proxy_handler(state: ProxyState) -> type[http.server.BaseHTTPRequestHandler]:
    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            pass

        def _reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for name, value in {**headers, "Content-Length": str(len(body))}.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if state.credentials is not None:
                token = base64.b64encode(":".join(state.credentials).encode()).decode()
                if self.headers.get("Proxy-Authorization") != f"Basic {token}":
                    self._reply(407, b"proxy auth required", {"Proxy-Authenticate": "Basic"})
                    return
            with state.lock:
                state.forwarded.append(self.path)
            try:
                with urllib.request.urlopen(self.path, timeout=5) as upstream:  # noqa: S310
                    content_type = upstream.headers.get("Content-Type", "text/html")
                    self._reply(upstream.status, upstream.read(), {"Content-Type": content_type})
            except OSError:
                self._reply(502, b"", {})

    return Proxy


@pytest.fixture
def start_proxy() -> Iterator[Callable[[tuple[str, str] | None], tuple[str, ProxyState]]]:
    """Start local forward proxies on demand; each returns its URL and state."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(credentials: tuple[str, str] | None) -> tuple[str, ProxyState]:
        state = ProxyState(credentials)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _proxy_handler(state))
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}", state

    yield start
    for server in servers:
        server.shutdown()


@dataclass(frozen=True)
class ProxyStep:
    """One fetch through the proxy named."""

    proxy: str
    fetch: dict[str, Any] = field(default_factory=dict)
    status: int = 200  # 407 when the proxy refuses the credentials


@dataclass(frozen=True)
class ProxyRun:
    """Proxies by name with the credentials each demands, what the client sends, the fetches."""

    demands: dict[str, tuple[str, str] | None]
    sends: str | None  # userinfo in the proxy URL, percent-encoded
    steps: tuple[ProxyStep, ...]


PROXIES: dict[str, ProxyRun] = {
    "carries_the_page": ProxyRun({"a": None}, None, (ProxyStep("a"),)),
    "authenticates_with_credentials": ProxyRun(
        {"a": ("bob", "s3cr3t")}, "bob:s3cr3t", (ProxyStep("a"),)
    ),
    "decodes_percent_encoded_credentials": ProxyRun(
        {"a": ("us er", "p@ss")}, "us%20er:p%40ss", (ProxyStep("a"),)
    ),
    # Chrome shows the proxy's 407 page at once; the target is never reached.
    "refuses_wrong_credentials": ProxyRun(
        {"a": ("bob", "right")}, "bob:WRONG", (ProxyStep("a", status=407),)
    ),
    "moves_to_a_new_proxy_at_runtime": ProxyRun(
        {"a": None, "b": None}, None, (ProxyStep("a"), ProxyStep("b"))
    ),
    # chromiumoxide owns the Fetch domain for proxy auth; nav blocking must not disable it.
    "keeps_authenticating_after_block_navigation": ProxyRun(
        {"a": ("u", "p")},
        "u:p",
        (ProxyStep("a", {"block_navigation": True}), ProxyStep("a")),
    ),
}


@pytest.mark.parametrize("name", list(PROXIES))
def test_proxy(
    httpserver: HTTPServer,
    start_proxy: Callable[[tuple[str, str] | None], tuple[str, ProxyState]],
    name: str,
) -> None:
    run = PROXIES[name]
    httpserver.expect_request("/").respond_with_data(
        "<html><body>PROXIED_OK</body></html>", content_type="text/html"
    )
    target = httpserver.url_for("/")
    proxies = {proxy: start_proxy(demands) for proxy, demands in run.demands.items()}
    userinfo = f"{run.sends}@" if run.sends else ""
    address = {
        proxy: url.replace("http://", f"http://{userinfo}") for proxy, (url, _) in proxies.items()
    }
    first = run.steps[0].proxy
    with onyxweb.Client(concurrency=1, proxy=address[first], proxy_bypass_list=_BYPASS) as c:
        for step in run.steps:
            c.config.network.proxy = address[step.proxy]
            before = {proxy: len(state.forwarded) for proxy, (_, state) in proxies.items()}
            r = c.fetch(target, **step.fetch)
            carried = {
                proxy: state.forwarded[before[proxy] :] for proxy, (_, state) in proxies.items()
            }
            assert r.status_code == step.status
            if step.status == 200:
                assert "PROXIED_OK" in r
                assert {p for p, uris in carried.items() if uris} == {step.proxy}
                assert target in carried[step.proxy]
            else:
                assert "PROXIED_OK" not in r
                assert not any(carried.values())
