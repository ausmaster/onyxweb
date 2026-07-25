"""onyxweb — URL → fully-rendered HTML (and/or screenshot) for Python.

Powered by Chromium via CDP. Under the hood it's a Rust/tokio-driven
chromiumoxide client speaking CDP directly to a bundled chrome-headless-shell.

Typical usage::

    import onyxweb

    # One-shot (uses a shared, process-wide default Client)
    html = onyxweb.fetch("https://example.com")
    png  = onyxweb.screenshot("https://example.com")
    both = onyxweb.fetch_all("https://example.com")

    # Explicit Client for batch / tuning
    with onyxweb.Client(concurrency=16) as client:
        for result in client.batch(urls, capture="both"):
            title = result.html.dom.title()
            ...

All HTML search (``.dom.query()``, ``.dom.find()``, etc.) runs in Rust for
speed; no Python HTML parsing round-trip.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel as _BaseModel

from onyxweb._logging import configure as _configure_logging, logger, set_log_level
from onyxweb._onyxweb import (
    Client as _RustClient,
    Dom as Dom,
    Element as Element,
    OnyxwebError as OnyxwebError,
    _FetchOutput,
    _RenderOutput,
)
from onyxweb.config import (
    ChromeConfig,
    Click,
    ClientConfig,
    EmulationConfig,
    FetchConfig,
    Fill,
    Hover,
    NetworkConfig,
    ScreenshotConfig,
    ScriptsConfig,
    TimeoutConfig,
    UserAgentBrandVersion,
    UserAgentMetadata,
    ViewportConfig,
    Wait,
)

# Configure Python-side logging at import from ONYXWEB_LOG (defaults "warn").
# The Rust side reads the same env var at PyO3 module init.
_configure_logging()

_client_log = logger.getChild("client")

__all__ = [
    # Module-level convenience — sync
    "fetch",
    "screenshot",
    "fetch_all",
    # Module-level convenience — async
    "afetch",
    "ascreenshot",
    "afetch_all",
    # Classes
    "AntiBot",
    "AsyncClient",
    "OnyxwebError",
    "CertInfo",
    "Click",
    "Client",
    "ConsoleMessage",
    "Dom",
    "Element",
    "FetchResult",
    "Fill",
    "Hashes",
    "Hover",
    "RedirectHop",
    "RenderResult",
    "ResponseHeaders",
    "ResponseMetadata",
    "Wait",
    # Configs (re-exported from onyxweb.config)
    "ClientConfig",
    "FetchConfig",
    "ScreenshotConfig",
    "ScriptsConfig",
    "ViewportConfig",
    "NetworkConfig",
    "EmulationConfig",
    "TimeoutConfig",
    "ChromeConfig",
    "UserAgentBrandVersion",
    "UserAgentMetadata",
    # Logging
    "logger",
    "set_log_level",
]


# Ensure the Rust side can locate the bundled chrome binary by pointing at this
# package's installed directory.
os.environ.setdefault(
    "ONYXWEB_PKG_DIR",
    os.path.dirname(os.path.abspath(__file__)),
)


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsoleMessage:
    """One ``console.*`` event captured during a page visit.

    Attributes:
        type: The console method that fired —
            ``"log"`` / ``"info"`` / ``"warning"`` / ``"error"`` /
            ``"debug"`` / ``"trace"``.
        text: The rendered message body (chrome stringifies any non-string
            arguments before delivering the event).
        timestamp: ``time.time()`` (seconds since epoch) at the moment the
            event was captured by onyxweb.
    """

    type: Literal["log", "info", "warning", "error", "debug", "trace"]
    text: str
    timestamp: float


@dataclass(frozen=True)
class RedirectHop:
    """One redirect hop on the way to the final main-document response.

    Attributes:
        url: The URL that responded with the redirect.
        status: That hop's HTTP status (e.g. ``301`` / ``302``).
        remote_ip: The IP that served the hop, or ``None``.
    """

    url: str
    status: int
    remote_ip: str | None


@dataclass(frozen=True)
class AntiBot:
    """A WAF / anti-bot measure encountered during the fetch.

    ``RenderResult.anti_bot`` is ``None`` when nothing was detected. Detection
    runs regardless of ``bypass_anti_bot`` — so this flags the WAF even if you
    didn't try to get past it (useful recon signal on its own).

    Attributes:
        vendor: Detected vendor — ``"akamai"`` / ``"cloudflare"`` /
            ``"datadome"`` / ``"perimeterx"`` / ``"imperva"`` / ``"aws"`` /
            ``"kasada"`` / ``"recaptcha"`` / ``"hcaptcha"`` — or ``None`` for a
            generic challenge with no identifiable vendor.
        kind: ``"challenge"`` (a JS interstitial or captcha gate) or ``"block"``
            (a hard 403/429/406). Interactive captchas are ``"challenge"`` with
            ``resolved=False`` — detected, but not auto-solvable.
        resolved: ``True`` if onyxweb got past it to the real page (the
            challenge cleared, or the self-heal recovered); ``False`` if the
            captured page is the challenge stub / block / captcha page.
    """

    vendor: str | None
    kind: Literal["challenge", "block"]
    resolved: bool


@dataclass(frozen=True)
class CertInfo:
    """TLS certificate details for the final HTTPS response.

    Extracted from CDP ``securityDetails``. ``None`` on ``ResponseMetadata``
    for plain HTTP. Mirrors blasthttp's ``CertInfo``.

    Attributes:
        common_name: Certificate subject Common Name.
        sans: Subject Alternative Names (DNS entries, and any others).
        emails: SAN entries that look like email addresses (contain ``@``).
        issuer: Issuer Common Name / distinguished name.
        not_before: Validity start, ISO 8601 (UTC).
        not_after: Validity end, ISO 8601 (UTC).
        fingerprint_sha256: SHA-256 fingerprint, or ``None`` — CDP's
            ``securityDetails`` doesn't expose it, so it's not captured (we
            never fabricate one).
    """

    common_name: str
    sans: list[str]
    emails: list[str]
    issuer: str
    not_before: str
    not_after: str
    fingerprint_sha256: str | None


def _epoch_to_iso(epoch: float) -> str:
    """Format epoch seconds as an ISO-8601 UTC timestamp."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


@dataclass(frozen=True)
class ResponseMetadata:
    """Structured metadata for the main-document HTTP response.

    Captured from CDP ``Network.responseReceived`` for the final main-document
    response (after any redirects). All fields are best-effort — a request that
    never produced a network response (e.g. a ``data:`` URL, or a navigation
    that failed before headers) yields zeros / empty strings / ``None``.

    Attributes:
        status_code: Final main-document HTTP status (0 if none was observed).
        status_text: Reason phrase, e.g. ``"OK"`` / ``"Not Found"``.
        mime_type: Browser-determined MIME type, e.g. ``"text/html"``.
        protocol: Negotiated protocol, e.g. ``"http/1.1"`` / ``"h2"`` / ``"h3"``.
            Empty when the browser reported none.
        remote_ip: Server IP the response came from, or ``None``.
        remote_port: Server port, or ``None``.
        content_length: Byte length of the rendered (post-JS) body — the same
            bytes exposed as ``str(result)``. Not the HTTP ``Content-Length``
            header.
        body_hashes: :class:`Hashes` over the rendered body bytes.
        request_url: The URL originally requested (before any redirects).
        request_method: HTTP method of the navigation — always ``"GET"``.
        redirect_chain: Ordered :class:`RedirectHop` list of redirect hops taken
            to reach the final response (empty when there were none).
        cert_info: :class:`CertInfo` for HTTPS responses, else ``None``.
        final_url: URL after any redirects (mirrors ``RenderResult.final_url``).
        elapsed_s: End-to-end page-visit time in seconds.
    """

    status_code: int
    status_text: str
    mime_type: str
    protocol: str
    remote_ip: str | None
    remote_port: int | None
    content_length: int
    body_hashes: Hashes
    request_url: str
    request_method: str
    redirect_chain: list[RedirectHop]
    cert_info: CertInfo | None
    final_url: str
    elapsed_s: float


@dataclass(frozen=True)
class Hashes:
    """md5 / mmh3 / sha256 digests over a byte string, computed in Rust.

    Byte-exact with Python's ``hashlib`` and ``mmh3`` — ``mmh3`` here is the
    signed 32-bit MurmurHash3 (x86_32, seed 0) that ``mmh3.hash()`` returns —
    so hashes correlate with tools (like BBOT) that hash the same bytes.

    Attributes:
        md5: Lowercase-hex MD5.
        mmh3: Signed 32-bit MurmurHash3 (x86_32, seed 0).
        sha256: Lowercase-hex SHA-256.
    """

    md5: str
    mmh3: int
    sha256: str


class ResponseHeaders(Mapping[str, str]):
    r"""Case-insensitive mapping of the main-document response headers.

    Behaves like a read-only ``dict`` — ``h["Content-Type"]`` /
    ``"server" in h`` / ``h.get(...)`` / iteration / ``dict(h)`` — with
    case-insensitive keys. Sourced from CDP
    ``Network.responseReceivedExtraInfo`` (the real received headers, including
    Set-Cookie), falling back to ``Network.responseReceived`` headers when
    extraInfo is unavailable.

    Attributes:
        raw: The canonical ``Name: Value\r\nName: Value`` header block — CRLF
            between entries, no status line, no pseudo-headers, no trailing
            CRLF. Matches ``blasthttp``'s ``raw_headers``, so it's a drop-in for
            BBOT and protocol-agnostic (same shape for HTTP/1.x and h2/h3). The
            real header names/values only — never fabricated framing.
        hashes: :class:`Hashes` over ``raw``'s bytes (md5 / mmh3 / sha256).
            Always present.
    """

    __slots__ = ("_pairs", "_ci", "raw", "hashes")

    def __init__(
        self,
        pairs: list[tuple[str, str]],
        raw: str,
        hashes: Hashes,
    ) -> None:
        """Build from Rust-supplied (name, value) pairs, raw text, and hashes."""
        self._pairs = list(pairs)
        # Case-insensitive single-value view (last duplicate wins). Duplicate
        # headers (Set-Cookie) are kept in `_pairs` for `.set_cookie`/`.cookies`.
        self._ci: dict[str, str] = {k.lower(): v for k, v in self._pairs}
        self.raw = raw
        self.hashes = hashes

    def __getitem__(self, key: str) -> str:
        return self._ci[key.lower()]

    def __iter__(self) -> Iterator[str]:
        # Unique keys, first-occurrence order (so dict(self) has one entry
        # per header name even when duplicates exist).
        seen: set[str] = set()
        for k, _ in self._pairs:
            low = k.lower()
            if low not in seen:
                seen.add(low)
                yield k

    def __len__(self) -> int:
        return len(self._ci)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._ci

    @property
    def set_cookie(self) -> list[str]:
        """All Set-Cookie header values, one per cookie."""
        return [v for k, v in self._pairs if k.lower() == "set-cookie"]

    @property
    def cookies(self) -> dict[str, str]:
        """Set-Cookie cookies parsed to ``name -> value``.

        Only the ``name=value`` before the first ``;`` is kept (``Path``,
        ``Expires``, ``HttpOnly``, etc. are stripped); on duplicate names the
        last Set-Cookie wins. Matches ``blasthttp``'s ``cookies``.
        """
        out: dict[str, str] = {}
        for sc in self.set_cookie:
            pair = sc.split(";", 1)[0]
            name, sep, value = pair.partition("=")
            if sep:
                out[name.strip()] = value.strip()
        return out

    def __repr__(self) -> str:
        return f"ResponseHeaders({dict(self)!r})"


def _make_response_headers(raw: _RenderOutput | _FetchOutput) -> ResponseHeaders:
    """Assemble a ``ResponseHeaders`` from a Rust raw output."""
    return ResponseHeaders(
        raw.headers,
        raw.header_raw,
        Hashes(md5=raw.header_md5, mmh3=raw.header_mmh3, sha256=raw.header_sha256),
    )


def _make_cert_info(raw: _RenderOutput | _FetchOutput) -> CertInfo | None:
    """Build ``CertInfo`` from the Rust cert tuple, or ``None`` for plain HTTP."""
    if raw.cert_info is None:
        return None
    common_name, sans, issuer, valid_from, valid_to = raw.cert_info
    return CertInfo(
        common_name=common_name,
        sans=list(sans),
        emails=[s for s in sans if "@" in s],
        issuer=issuer,
        not_before=_epoch_to_iso(valid_from),
        not_after=_epoch_to_iso(valid_to),
        fingerprint_sha256=None,
    )


def _make_response_metadata(raw: _RenderOutput | _FetchOutput) -> ResponseMetadata:
    """Assemble a ``ResponseMetadata`` from a Rust raw output."""
    return ResponseMetadata(
        status_code=raw.status_code,
        status_text=raw.status_text,
        mime_type=raw.mime_type,
        protocol=raw.protocol,
        remote_ip=raw.remote_ip,
        remote_port=raw.remote_port,
        content_length=raw.content_length,
        body_hashes=Hashes(md5=raw.body_md5, mmh3=raw.body_mmh3, sha256=raw.body_sha256),
        request_url=raw.request_url,
        request_method="GET",
        redirect_chain=[
            RedirectHop(url=u, status=s, remote_ip=ip) for (u, s, ip) in raw.redirect_chain
        ],
        cert_info=_make_cert_info(raw),
        final_url=raw.final_url,
        elapsed_s=raw.elapsed_s,
    )


def _make_anti_bot(raw: _RenderOutput | _FetchOutput) -> AntiBot | None:
    """Build ``AntiBot`` from the Rust ``(vendor, kind, resolved)`` tuple, or None."""
    if raw.anti_bot is None:
        return None
    vendor, kind, resolved = raw.anti_bot
    return AntiBot(vendor=vendor, kind=kind, resolved=resolved)


def _make_render_result(raw: _RenderOutput | _FetchOutput) -> RenderResult:
    """Build a ``RenderResult`` from a Rust raw output.

    Accepts both ``_RenderOutput`` and ``_FetchOutput`` via duck typing —
    each carries the same ``html`` / ``console_messages`` / ``final_url`` /
    ``status_code`` / ``elapsed_s`` / ``make_dom()`` / ``post_load_results``
    shape. ``errors`` is derived from ``console_messages`` for backward
    compatibility. ``post_load_results`` arrives as JSON strings from Rust;
    decoded to native Python here.
    """
    console_messages = [
        ConsoleMessage(type=m.type, text=m.text, timestamp=m.timestamp)
        for m in raw.console_messages
    ]
    errors = [m.text for m in console_messages if m.type == "error"]
    post_load_results = [json.loads(s) if s is not None else None for s in raw.post_load_results]
    return RenderResult(
        raw.html,
        errors=errors,
        console_messages=console_messages,
        final_url=raw.final_url,
        status_code=raw.status_code,
        elapsed_s=raw.elapsed_s,
        post_load_results=post_load_results,
        metadata=_make_response_metadata(raw),
        headers=_make_response_headers(raw),
        anti_bot=_make_anti_bot(raw),
        _raw=raw,
    )


class RenderResult(str):
    """Fully-rendered post-JS HTML. Subclasses ``str`` (lxml, regex, BS4 work).

    Adds:
      - ``.errors`` — list[str] of console errors and load errors
      - ``.console_messages`` — list[ConsoleMessage] captured during the visit
      - ``.final_url`` — URL after any redirects
      - ``.status_code`` — final HTTP status
      - ``.elapsed_s`` — end-to-end page-visit time (seconds)
      - ``.post_load_results`` — JS return values from each
        ``FetchConfig.post_load_scripts`` entry (None for undefined /
        non-JSON-serializable returns)
      - ``.metadata`` — :class:`ResponseMetadata` (status_text, mime_type,
        protocol, remote_ip/port, content_length, ...)
      - ``.headers`` — :class:`ResponseHeaders` (case-insensitive mapping of
        the response headers, ``.set_cookie`` / ``.cookies``, canonical
        ``.raw``, ``.hashes``)
      - ``.dom`` — Rust-side HTML query (lazy; CSS selectors + BS4-like find)
    """

    errors: list[str]
    console_messages: list[ConsoleMessage]
    final_url: str
    status_code: int
    elapsed_s: float
    post_load_results: list[Any]
    metadata: ResponseMetadata
    headers: ResponseHeaders
    anti_bot: AntiBot | None
    _raw: _RenderOutput | _FetchOutput | None
    _dom: Dom | None

    def __new__(
        cls,
        html: str,
        *,
        errors: list[str] | None = None,
        console_messages: list[ConsoleMessage] | None = None,
        final_url: str = "",
        status_code: int = 0,
        elapsed_s: float = 0.0,
        post_load_results: list[Any] | None = None,
        metadata: ResponseMetadata | None = None,
        headers: ResponseHeaders | None = None,
        anti_bot: AntiBot | None = None,
        _raw: _RenderOutput | _FetchOutput | None = None,
    ) -> RenderResult:
        """Construct a RenderResult; ``_raw`` is internal (Rust output object)."""
        instance = super().__new__(cls, html)
        instance.errors = errors or []
        instance.console_messages = console_messages or []
        instance.final_url = final_url
        instance.status_code = status_code
        instance.elapsed_s = elapsed_s
        instance.post_load_results = post_load_results or []
        instance.anti_bot = anti_bot
        instance.headers = (
            headers
            if headers is not None
            else ResponseHeaders([], "", Hashes(md5="", mmh3=0, sha256=""))
        )
        instance.metadata = metadata or ResponseMetadata(
            status_code=status_code,
            status_text="",
            mime_type="",
            protocol="",
            remote_ip=None,
            remote_port=None,
            content_length=0,
            body_hashes=Hashes(md5="", mmh3=0, sha256=""),
            request_url=final_url,
            request_method="GET",
            redirect_chain=[],
            cert_info=None,
            final_url=final_url,
            elapsed_s=elapsed_s,
        )
        instance._raw = _raw
        instance._dom = None
        return instance

    @property
    def html(self) -> str:
        """The raw rendered HTML as a plain ``str`` (same as ``str(self)``)."""
        return str(self)

    @property
    def dom(self) -> Dom:
        """Rust-parsed DOM (lazy). First access triggers html5ever parse."""
        dom = self._dom
        if dom is None:
            if self._raw is None:
                raise AttributeError(
                    "this RenderResult was not produced by onyxweb; .dom unavailable"
                )
            dom = self._raw.make_dom()
            object.__setattr__(self, "_dom", dom)
        return dom

    def __repr__(self) -> str:
        trunc = str(self)[:60] + "…" if len(self) > 60 else str(self)
        parts = [f"html={trunc!r}"]
        if self.final_url:
            parts.append(f"final_url={self.final_url!r}")
        if self.errors:
            parts.append(f"errors=[{len(self.errors)}]")
        return f"RenderResult({', '.join(parts)})"


class FetchResult:
    """HTML + PNG from one page visit. Use when you want both."""

    __slots__ = ("html", "png", "_raw")

    html: RenderResult
    png: bytes
    _raw: _FetchOutput

    def __init__(self, raw: _FetchOutput) -> None:
        self._raw = raw
        self.html = _make_render_result(raw)
        self.png = bytes(raw.png)

    @property
    def errors(self) -> list[str]:
        """Error texts (derived from ``console_messages``)."""
        return self.html.errors

    @property
    def console_messages(self) -> list[ConsoleMessage]:
        """All captured ``console.*`` events, structured."""
        return self.html.console_messages

    @property
    def final_url(self) -> str:
        """URL the browser ended up at, after any redirects."""
        return self._raw.final_url

    @property
    def status_code(self) -> int:
        """Final HTTP status code of the main document response."""
        return self._raw.status_code

    @property
    def elapsed_s(self) -> float:
        """End-to-end page-visit time in seconds."""
        return self._raw.elapsed_s

    @property
    def metadata(self) -> ResponseMetadata:
        """Structured response metadata (delegates to ``.html.metadata``)."""
        return self.html.metadata

    @property
    def headers(self) -> ResponseHeaders:
        """Response headers (delegates to ``.html.headers``)."""
        return self.html.headers

    @property
    def anti_bot(self) -> AntiBot | None:
        """WAF/anti-bot indicator, or ``None`` (delegates to ``.html.anti_bot``)."""
        return self.html.anti_bot

    def __repr__(self) -> str:
        return (
            f"FetchResult(html=<{len(self.html)} chars>, png=<{len(self.png)} bytes>, "
            f"final_url={self.final_url!r}, elapsed_s={self.elapsed_s:.3f})"
        )


class _RenderOutputShim:
    """Bridges FetchResult's raw into RenderResult.dom — both types have make_dom()."""

    def __init__(self, raw: _FetchOutput) -> None:
        self._raw = raw

    def make_dom(self) -> Dom:
        return self._raw.make_dom()


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------


#: Dotted paths into ClientConfig that can only be set at Client creation.
#: Attempting to change any of these via ``client.update_config(...)`` raises
#: ``ValueError`` — you must construct a new Client.
_LAUNCH_ONLY_FIELDS: tuple[tuple[str, ...], ...] = (
    ("concurrency",),              # Semaphore sized once at launch
    ("chrome", "path"),            # Chrome binary is already exec'd
    ("chrome", "args"),            # Chrome CLI flags fixed at launch
    ("chrome", "user_data_dir"),   # Chrome user-data-dir is per-process
    ("chrome", "headless"),        # ditto
    ("chrome", "engine"),          # binary is chosen + exec'd at launch
    ("network", "ignore_https_errors"),  # --ignore-certificate-errors is a CLI flag
    ("timeout", "launch_ms"),      # only meaningful before Chrome is up
)


class Client:
    """Long-lived chromium connection backed by a pre-warmed page pool.

    Thread-safe — N Python threads may call ``fetch()``/``screenshot()``/
    ``batch()`` concurrently, capped by ``concurrency``.
    """

    __slots__ = ("_rust", "_config")

    def __init__(
        self,
        *args: Any,
        config: ClientConfig | None = None,
        **kwargs: Any,
    ) -> None:
        if args:
            raise TypeError(
                "Client() takes only keyword args. Pass config=ClientConfig(...) "
                "or flat kwargs like Client(viewport=(w,h), concurrency=N, ...)."
            )
        if config is not None and kwargs:
            raise TypeError("pass either config=... or flat kwargs, not both")

        if config is None:
            config = ClientConfig.from_flat(**kwargs) if kwargs else ClientConfig()

        self._config = config
        _client_log.info(
            "Client init: concurrency=%d viewport=%dx%d",
            config.concurrency,
            config.viewport.width,
            config.viewport.height,
        )
        self._rust = _RustClient(config.model_dump())

    # --- Config introspection + runtime update ---------------------------

    @property
    def config(self) -> _ConfigView:
        """Live-mutable config view.

        ``client.config.network.user_agent = "X"`` at any depth auto-syncs to
        Rust. Launch-only fields raise ``ValueError`` at the assignment line.
        Call ``.snapshot()`` for a detached deep-copy.
        """
        return _ConfigView(self, ())

    def update_config(
        self,
        *args: Any,
        config: ClientConfig | None = None,
        **kwargs: Any,
    ) -> None:
        """Swap in new config (takes effect on next fetch).

        Pass either ``config=ClientConfig(...)`` OR flat kwargs. In-flight
        calls snapshot at start so won't see a torn state. Raises
        ``ValueError`` on any launch-only field change (see
        ``_LAUNCH_ONLY_FIELDS``).
        """
        if args:
            raise TypeError("update_config() takes only keyword args")
        if config is not None and kwargs:
            raise TypeError("pass either config= OR flat kwargs, not both")

        if config is not None:
            new_config = config
        elif kwargs:
            partial = _flat_kwargs_to_partial(kwargs)
            merged = _deep_merge(self._config.model_dump(), partial)
            new_config = ClientConfig.model_validate(merged)
        else:
            return

        self._apply_config(new_config)

    def _apply_config(self, new_config: ClientConfig) -> None:
        """Validate launch-only invariants, push to Rust, store new config.

        Used by both ``update_config()`` and the ``_ConfigView`` attribute proxy.
        """
        old_data = self._config.model_dump()
        new_data = new_config.model_dump()
        for path in _LAUNCH_ONLY_FIELDS:
            if _get_nested(old_data, path) != _get_nested(new_data, path):
                raise ValueError(
                    f"cannot change launch-only field {'.'.join(path)!r} at runtime "
                    f"(was {_get_nested(old_data, path)!r}, "
                    f"requested {_get_nested(new_data, path)!r}). "
                    f"Create a new Client to change this setting."
                )
        self._rust.update_config(new_data)
        self._config = new_config

    # --- Primary API -------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        config: FetchConfig | None = None,
        **overrides: Any,
    ) -> RenderResult:
        """Fetch URL, return fully-rendered HTML post-JS."""
        fc = _merge_fetch_config(config, overrides)
        _client_log.debug("fetch: %s", url)
        return _make_render_result(self._rust.fetch(url, fc.model_dump()))

    def screenshot(
        self,
        url: str,
        *,
        config: ScreenshotConfig | None = None,
        **overrides: Any,
    ) -> bytes:
        """Fetch URL, return a screenshot as image bytes (PNG by default)."""
        if config is None and not overrides:
            sc = ScreenshotConfig()
        elif config is not None and not overrides:
            sc = config
        else:
            data = config.model_dump() if config else {}
            for k, v in overrides.items():
                if k not in _SCREENSHOT_KWARGS:
                    raise TypeError(f"unknown screenshot kwarg: {k!r}")
                data[k] = v
            sc = ScreenshotConfig.model_validate(data)
        _client_log.debug("screenshot: %s (format=%s)", url, sc.format)
        return bytes(self._rust.screenshot(url, sc.model_dump()))

    def fetch_all(
        self,
        url: str,
        *,
        config: FetchConfig | None = None,
        full_page: bool = False,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int | None = None,
        **overrides: Any,
    ) -> FetchResult:
        """Fetch URL, return HTML + image bytes from one page visit.

        ``format`` picks the image encoding; ``quality`` is 0-100 for jpeg/webp
        (ignored for png). The encoded bytes land on ``FetchResult.png`` (field
        name is historical — it holds whatever format you asked for).
        """
        fc = _merge_fetch_config(config, overrides)
        sc = ScreenshotConfig(full_page=full_page, format=format, quality=quality)
        _client_log.debug("fetch_all: %s (format=%s)", url, format)
        raw = self._rust.fetch_all(url, fc.model_dump(), sc.model_dump())
        return FetchResult(raw)

    def batch(
        self,
        urls: Iterable[str],
        *,
        capture: Literal["html", "png", "both"] = "html",
        config: FetchConfig | None = None,
    ) -> list[RenderResult | FetchResult | bytes | Exception]:
        """Run a batch of URLs in parallel (tokio-driven). Returns when all complete.

        Return type depends on ``capture`` (``"html"`` → RenderResult, ``"png"``
        → bytes, ``"both"`` → FetchResult), positionally aligned with ``urls``.

        A URL that fails is returned **in place** as the exception instance a
        single fetch would raise (``TimeoutError`` for timeouts, ``OnyxwebError``
        otherwise), carrying ``.url`` (which URL) and ``.kind`` (the cause
        category). Detect with ``isinstance(item, Exception)`` — one bad URL
        never sinks the rest of the batch.
        """
        fc = config or FetchConfig()
        url_list = list(urls)
        _client_log.info("batch: %d URLs, capture=%s", len(url_list), capture)
        raws = self._rust.batch(url_list, capture, fc.model_dump())
        results: list[RenderResult | FetchResult | bytes | Exception]
        if capture == "html":
            results = [r if isinstance(r, Exception) else _make_render_result(r) for r in raws]
        elif capture == "png":
            results = [r if isinstance(r, Exception) else bytes(r) for r in raws]
        else:
            results = [r if isinstance(r, Exception) else FetchResult(r) for r in raws]
        _client_log.debug("batch done: %d results returned", len(results))
        return results

    # --- Private / experimental -------------------------------------------

    def _render(
        self,
        html: bytes | str,
        *,
        base_url: str | None = None,
        config: FetchConfig | None = None,
    ) -> RenderResult:
        """NOT public. Inject raw HTML into chromium via data: URL.

        Niche: most users want ``.fetch(url)``. This is kept because it's cheap
        to implement (data: URL) and might be useful for unit tests.
        """
        if isinstance(html, str):
            html = html.encode("utf-8")
        import base64 as _b64

        data_url = "data:text/html;base64," + _b64.b64encode(html).decode("ascii")
        # base_url is not honored — would need document.write or a <base> tag.
        del base_url
        return self.fetch(data_url, config=config)

    # --- Lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Tear down the chromium process and free pool resources."""
        _client_log.info("Client close")
        self._rust.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ----------------------------------------------------------------------------
# Module-level convenience (shared default Client, lazy-init, thread-safe)
# ----------------------------------------------------------------------------

_default_client: Client | None = None
_default_client_lock = threading.Lock()


def _get_default_client() -> Client:
    global _default_client
    if _default_client is None:
        with _default_client_lock:
            if _default_client is None:
                _default_client = Client()
    return _default_client


def fetch(url: str, *, config: FetchConfig | None = None, **overrides: Any) -> RenderResult:
    """Fetch URL → fully-rendered HTML. Uses a shared default Client."""
    return _get_default_client().fetch(url, config=config, **overrides)


def screenshot(
    url: str, *, config: ScreenshotConfig | None = None, **overrides: Any
) -> bytes:
    """Fetch URL → PNG bytes. Uses a shared default Client."""
    return _get_default_client().screenshot(url, config=config, **overrides)


def fetch_all(
    url: str,
    *,
    config: FetchConfig | None = None,
    full_page: bool = False,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int | None = None,
    **overrides: Any,
) -> FetchResult:
    """Fetch URL → HTML + image bytes. Uses a shared default Client."""
    return _get_default_client().fetch_all(
        url,
        config=config,
        full_page=full_page,
        format=format,
        quality=quality,
        **overrides,
    )


# ----------------------------------------------------------------------------
# AsyncClient — async peer of Client
# ----------------------------------------------------------------------------


class AsyncClient:
    """Async peer of :class:`Client`. Same API, methods return coroutines.

    Use as an async context manager (``async with``) or call :meth:`aclose`
    explicitly. Multiple coroutines on one event loop can ``await`` fetch /
    screenshot calls concurrently — the page-pool semaphore caps in-flight
    pages at ``concurrency``.

    Construction is sync (chromium subprocess spawn briefly blocks the event
    loop). Match :class:`Client`'s signature: pass ``config=ClientConfig(...)``
    or flat kwargs.

    Example:
        >>> import asyncio, onyxweb
        >>>
        >>> async def main():
        ...     async with onyxweb.AsyncClient() as ac:
        ...         result = await ac.fetch("https://example.com")
        ...         print(result.dom.title())
        >>>
        >>> asyncio.run(main())
    """

    __slots__ = ("_rust", "_config")

    def __init__(
        self,
        *args: Any,
        config: ClientConfig | None = None,
        **kwargs: Any,
    ) -> None:
        """Construct an AsyncClient.

        Args:
            *args: Reserved for keyword-only enforcement. Passing any
                positional args raises ``TypeError``.
            config: A pre-built ``ClientConfig``. Mutually exclusive with
                ``**kwargs``.
            **kwargs: Flat config kwargs (``viewport=(w, h)``,
                ``concurrency=N``, ``user_agent=...``, etc.). See
                :meth:`ClientConfig.from_flat`. Mutually exclusive with
                ``config``.

        Raises:
            TypeError: If positional args are passed, or if both ``config``
                and flat kwargs are given.
        """
        if args:
            raise TypeError(
                "AsyncClient() takes only keyword args. Pass config=ClientConfig(...) "
                "or flat kwargs like AsyncClient(viewport=(w,h), concurrency=N, ...)."
            )
        if config is not None and kwargs:
            raise TypeError("pass either config=... or flat kwargs, not both")

        if config is None:
            config = ClientConfig.from_flat(**kwargs) if kwargs else ClientConfig()

        self._config = config
        _client_log.info(
            "AsyncClient init: concurrency=%d viewport=%dx%d",
            config.concurrency,
            config.viewport.width,
            config.viewport.height,
        )
        self._rust = _RustClient(config.model_dump())

    # --- Config introspection + runtime update ---------------------------

    @property
    def config(self) -> _ConfigView:
        """Live-mutable config view.

        ``ac.config.network.user_agent = "X"`` at any depth auto-syncs to
        Rust. Launch-only fields raise ``ValueError`` at the assignment
        line. Call ``.snapshot()`` for a detached deep-copy.
        """
        return _ConfigView(self, ())

    def update_config(
        self,
        *args: Any,
        config: ClientConfig | None = None,
        **kwargs: Any,
    ) -> None:
        """Swap in new config (takes effect on next fetch).

        Sync — config validation only, no IO. Pass ``config=ClientConfig(...)``
        OR flat kwargs. In-flight calls snapshot at start so they don't see
        a torn state.

        Raises:
            ValueError: On any launch-only field change. Create a new
                AsyncClient instead.
            TypeError: If both ``config`` and flat kwargs are given.
        """
        if args:
            raise TypeError("update_config() takes only keyword args")
        if config is not None and kwargs:
            raise TypeError("pass either config= OR flat kwargs, not both")

        if config is not None:
            new_config = config
        elif kwargs:
            partial = _flat_kwargs_to_partial(kwargs)
            merged = _deep_merge(self._config.model_dump(), partial)
            new_config = ClientConfig.model_validate(merged)
        else:
            return

        self._apply_config(new_config)

    def _apply_config(self, new_config: ClientConfig) -> None:
        """Validate launch-only invariants, push to Rust, store new config."""
        old_data = self._config.model_dump()
        new_data = new_config.model_dump()
        for path in _LAUNCH_ONLY_FIELDS:
            if _get_nested(old_data, path) != _get_nested(new_data, path):
                raise ValueError(
                    f"cannot change launch-only field {'.'.join(path)!r} at runtime "
                    f"(was {_get_nested(old_data, path)!r}, "
                    f"requested {_get_nested(new_data, path)!r}). "
                    f"Create a new AsyncClient to change this setting."
                )
        self._rust.update_config(new_data)
        self._config = new_config

    # --- Primary API ------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        config: FetchConfig | None = None,
        **overrides: Any,
    ) -> RenderResult:
        """Fetch URL, return fully-rendered HTML post-JS.

        Args:
            url: The URL to fetch.
            config: A ``FetchConfig`` for this call. Mutually exclusive with
                ``**overrides``.
            **overrides: Per-call overrides
                (``extra_headers``, ``timeout_ms``, ``wait_until``,
                ``wait_after_ms``).

        Returns:
            ``RenderResult`` — a ``str`` subclass holding the rendered HTML
            plus ``.errors`` / ``.final_url`` / ``.status_code`` /
            ``.elapsed_s`` / ``.dom``.

        Raises:
            RuntimeError: On CDP / navigation failures.
        """
        fc = _merge_fetch_config(config, overrides)
        _client_log.debug("afetch: %s", url)
        return _make_render_result(await self._rust.fetch_async(url, fc.model_dump()))

    async def screenshot(
        self,
        url: str,
        *,
        config: ScreenshotConfig | None = None,
        **overrides: Any,
    ) -> bytes:
        """Fetch URL, return a screenshot as image bytes (PNG by default).

        Args:
            url: The URL to fetch.
            config: A ``ScreenshotConfig`` for this call. Mutually exclusive
                with ``**overrides``.
            **overrides: Per-call overrides matching ``ScreenshotConfig``
                fields (``viewport``, ``full_page``, ``format``, ``quality``,
                etc.).

        Returns:
            Image bytes in the requested format.

        Raises:
            TypeError: On unknown screenshot kwarg.
            RuntimeError: On CDP / navigation failures.
        """
        if config is None and not overrides:
            sc = ScreenshotConfig()
        elif config is not None and not overrides:
            sc = config
        else:
            data = config.model_dump() if config else {}
            for k, v in overrides.items():
                if k not in _SCREENSHOT_KWARGS:
                    raise TypeError(f"unknown screenshot kwarg: {k!r}")
                data[k] = v
            sc = ScreenshotConfig.model_validate(data)
        _client_log.debug("ascreenshot: %s (format=%s)", url, sc.format)
        return bytes(await self._rust.screenshot_async(url, sc.model_dump()))

    async def fetch_all(
        self,
        url: str,
        *,
        config: FetchConfig | None = None,
        full_page: bool = False,
        format: Literal["png", "jpeg", "webp"] = "png",
        quality: int | None = None,
        **overrides: Any,
    ) -> FetchResult:
        """Fetch URL, return HTML + image bytes from one page visit.

        Args:
            url: The URL to fetch.
            config: A ``FetchConfig`` for this call.
            full_page: Capture the entire scrollable page, not just the
                viewport.
            format: Image encoding — ``"png"`` (default), ``"jpeg"``,
                or ``"webp"``.
            quality: 0-100 for jpeg/webp; ignored for png.
            **overrides: Per-call ``FetchConfig`` overrides.

        Returns:
            ``FetchResult`` with ``.html`` (RenderResult) and ``.png``
            (image bytes — field name is historical; holds whatever
            ``format`` was requested).

        Raises:
            RuntimeError: On CDP / navigation failures.
        """
        fc = _merge_fetch_config(config, overrides)
        sc = ScreenshotConfig(full_page=full_page, format=format, quality=quality)
        _client_log.debug("afetch_all: %s (format=%s)", url, format)
        raw = await self._rust.fetch_all_async(url, fc.model_dump(), sc.model_dump())
        return FetchResult(raw)

    async def batch(
        self,
        urls: Iterable[str],
        *,
        capture: Literal["html", "png", "both"] = "html",
        config: FetchConfig | None = None,
    ) -> list[RenderResult | FetchResult | bytes | Exception]:
        """Run a batch of URLs in parallel (tokio-driven). Awaits all.

        Args:
            urls: Iterable of URLs to fetch.
            capture: ``"html"`` → list[RenderResult], ``"png"`` → list[bytes],
                ``"both"`` → list[FetchResult].
            config: A ``FetchConfig`` applied to every URL in the batch.

        Returns:
            List of results in input order. A URL that fails is returned **in
            place** as the exception instance a single fetch would raise
            (``TimeoutError`` for timeouts, ``OnyxwebError`` otherwise), carrying
            ``.url`` and ``.kind``. Detect with ``isinstance(item, Exception)``;
            one bad URL never aborts the batch.

        Raises:
            ValueError: If ``capture`` is not one of the three valid values.
        """
        fc = config or FetchConfig()
        url_list = list(urls)
        _client_log.info("abatch: %d URLs, capture=%s", len(url_list), capture)
        raws = await self._rust.batch_async(url_list, capture, fc.model_dump())
        results: list[RenderResult | FetchResult | bytes | Exception]
        if capture == "html":
            results = [r if isinstance(r, Exception) else _make_render_result(r) for r in raws]
        elif capture == "png":
            results = [r if isinstance(r, Exception) else bytes(r) for r in raws]
        else:
            results = [r if isinstance(r, Exception) else FetchResult(r) for r in raws]
        _client_log.debug("abatch done: %d results returned", len(results))
        return results

    # --- Lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Tear down the chromium process and free pool resources.

        Idempotent — calling on an already-closed AsyncClient is a no-op.
        """
        _client_log.info("AsyncClient aclose")
        await self._rust.close_async()

    async def __aenter__(self) -> AsyncClient:
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Exit the async context manager — calls :meth:`aclose`."""
        await self.aclose()


# ----------------------------------------------------------------------------
# Module-level async convenience (shared default AsyncClient, lazy-init)
# ----------------------------------------------------------------------------

_default_async_client: AsyncClient | None = None
_default_async_client_lock = threading.Lock()


def _get_default_async_client() -> AsyncClient:
    """Return (or lazily build) the shared module-level AsyncClient."""
    global _default_async_client
    if _default_async_client is None:
        with _default_async_client_lock:
            if _default_async_client is None:
                _default_async_client = AsyncClient()
    return _default_async_client


async def afetch(
    url: str, *, config: FetchConfig | None = None, **overrides: Any
) -> RenderResult:
    """Async fetch URL → fully-rendered HTML. Uses a shared default AsyncClient.

    See :meth:`AsyncClient.fetch` for arguments.
    """
    return await _get_default_async_client().fetch(url, config=config, **overrides)


async def ascreenshot(
    url: str, *, config: ScreenshotConfig | None = None, **overrides: Any
) -> bytes:
    """Async fetch URL → image bytes. Uses a shared default AsyncClient.

    See :meth:`AsyncClient.screenshot` for arguments.
    """
    return await _get_default_async_client().screenshot(url, config=config, **overrides)


async def afetch_all(
    url: str,
    *,
    config: FetchConfig | None = None,
    full_page: bool = False,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: int | None = None,
    **overrides: Any,
) -> FetchResult:
    """Async fetch URL → HTML + image bytes. Uses a shared default AsyncClient.

    See :meth:`AsyncClient.fetch_all` for arguments.
    """
    return await _get_default_async_client().fetch_all(
        url,
        config=config,
        full_page=full_page,
        format=format,
        quality=quality,
        **overrides,
    )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


_SCREENSHOT_KWARGS = {
    "viewport",
    "full_page",
    "timeout_ms",
    "extra_headers",
    "format",
    "quality",
    "wait_until",
    "wait_after_ms",
    "wait_after_post_load_ms",
}


def _merge_fetch_config(base: FetchConfig | None, overrides: dict[str, Any]) -> FetchConfig:
    if base is None and not overrides:
        return FetchConfig()
    data: dict[str, Any] = base.model_dump() if base else {}
    for k, v in overrides.items():
        if k not in {
            "actions",
            "block_navigation",
            "block_urls",
            "extra_headers",
            "bypass_anti_bot",
            "post_load_scripts",
            "scripts",
            "timeout_ms",
            "wait_until",
            "wait_after_ms",
            "wait_after_post_load_ms",
        }:
            raise TypeError(f"unknown fetch kwarg: {k!r}")
        data[k] = v
    return FetchConfig.model_validate(data)


# ----------------------------------------------------------------------------
# Live-mutable config view — returned by Client.config / AsyncClient.config
# ----------------------------------------------------------------------------


class _ClientLike(Protocol):
    """Internal: structural type for ``_ConfigView``.

    Both ``Client`` and ``AsyncClient`` satisfy this — they share the
    config-view machinery despite differing in their fetch/screenshot
    return types.
    """

    _config: ClientConfig

    def _apply_config(self, new_config: ClientConfig) -> None: ...


class _ConfigView:
    """Live proxy over a client's config.

    Reads delegate to the pydantic model; writes route through
    ``client._apply_config`` to keep Rust in sync. Only ``_client`` /
    ``_path`` live on instances (``__slots__``).
    """

    __slots__ = ("_client", "_path")

    def __init__(self, client: _ClientLike, path: tuple[str, ...]) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_path", path)

    def _target(self) -> Any:
        """Walk current pydantic config down ``self._path`` and return the node."""
        cur: Any = self._client._config  # noqa: SLF001
        for p in self._path:
            cur = getattr(cur, p)
        return cur

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        val = getattr(self._target(), name)
        if isinstance(val, _BaseModel):
            # Nested sub-config — return a view one level deeper so mutations
            # at any depth still route through _apply_config.
            return _ConfigView(self._client, self._path + (name,))
        return val

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        # Build a sparse partial dict for THIS change:
        #   path=("network",), name="user_agent" → {"network": {"user_agent": value}}
        partial: dict[str, Any] = {}
        cur = partial
        for p in self._path:
            cur[p] = {}
            cur = cur[p]
        cur[name] = value
        merged = _deep_merge(self._client._config.model_dump(), partial)  # noqa: SLF001
        new_cfg = ClientConfig.model_validate(merged)
        self._client._apply_config(new_cfg)  # noqa: SLF001

    def __repr__(self) -> str:
        return f"<live config view of {self._target()!r}>"

    def snapshot(self) -> ClientConfig | Any:
        """Detached deep-copy. Sub-views return their sub-config type."""
        return self._target().model_copy(deep=True)

    def model_dump(self, **kw: Any) -> dict[str, Any]:
        # _target() returns the live pydantic model whose typing varies by depth.
        return self._target().model_dump(**kw)  # type: ignore[no-any-return]

    def model_dump_json(self, **kw: Any) -> str:
        return self._target().model_dump_json(**kw)  # type: ignore[no-any-return]


# update_config / Client(**kwargs) build a SPARSE partial dict (unlike
# ClientConfig.from_flat, which fills defaults) — kept as a separate table.
_FLAT_KWARG_MAP: dict[str, tuple[str, ...]] = {
    # Viewport
    "device_scale_factor": ("viewport", "device_scale_factor"),
    "mobile": ("viewport", "mobile"),
    # Network
    "user_agent": ("network", "user_agent"),
    "user_agent_metadata": ("network", "user_agent_metadata"),
    "proxy": ("network", "proxy"),
    "proxy_bypass_list": ("network", "proxy_bypass_list"),
    "extra_headers": ("network", "extra_headers"),
    "ignore_https_errors": ("network", "ignore_https_errors"),
    "block_urls": ("network", "block_urls"),
    "disable_cache": ("network", "disable_cache"),
    "offline": ("network", "offline"),
    "latency_ms": ("network", "latency_ms"),
    "download_bps": ("network", "download_bps"),
    "upload_bps": ("network", "upload_bps"),
    # Emulation
    "locale": ("emulation", "locale"),
    "timezone": ("emulation", "timezone"),
    "geolocation": ("emulation", "geolocation"),
    "prefers_color_scheme": ("emulation", "prefers_color_scheme"),
    "javascript_enabled": ("emulation", "javascript_enabled"),
    # Timeout
    "navigation_timeout_ms": ("timeout", "navigation_ms"),
    "launch_timeout_ms": ("timeout", "launch_ms"),
    "screenshot_timeout_ms": ("timeout", "screenshot_ms"),
    # Chrome
    "chrome_path": ("chrome", "path"),
    "chrome_args": ("chrome", "args"),
    "user_data_dir": ("chrome", "user_data_dir"),
    "headless": ("chrome", "headless"),
    "engine": ("chrome", "engine"),
}


def _flat_kwargs_to_partial(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Translate flat kwargs into a sparse nested dict.

    Only mentioned fields appear in the output; defaults are NOT filled in.
    Meant for merging onto an existing config.
    """
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if k == "viewport":
            if isinstance(v, tuple) and len(v) == 2:
                out.setdefault("viewport", {})
                out["viewport"]["width"] = int(v[0])
                out["viewport"]["height"] = int(v[1])
            elif isinstance(v, ViewportConfig):
                out["viewport"] = v.model_dump()
            else:
                raise TypeError(
                    f"viewport must be (w,h) or ViewportConfig, got {type(v).__name__}"
                )
            continue
        if k == "scripts":
            if isinstance(v, ScriptsConfig):
                out["scripts"] = v.model_dump()
            elif isinstance(v, dict):
                out["scripts"] = dict(v)
            else:
                raise TypeError(
                    f"scripts must be dict or ScriptsConfig, got {type(v).__name__}"
                )
            continue
        if k == "concurrency":
            out["concurrency"] = v
            continue
        if k == "wait_until":
            out["wait_until"] = v
            continue
        if k == "wait_after_ms":
            out["wait_after_ms"] = v
            continue
        if k == "capture_console_level":
            out["capture_console_level"] = v
            continue
        if k == "bypass_anti_bot":
            out["bypass_anti_bot"] = v
            continue
        if k not in _FLAT_KWARG_MAP:
            raise TypeError(f"unknown ClientConfig kwarg: {k!r}")
        sub, field = _FLAT_KWARG_MAP[k]
        out.setdefault(sub, {})
        out[sub][field] = v
    return out


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` into `base`. `overlay` wins where both have a key."""
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _get_nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Walk a dotted path into a nested dict. Returns None if any step is missing."""
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur
