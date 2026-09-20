"""onyxweb — URL → fully-rendered HTML (and/or screenshot) for Python.

Powered by Chromium via CDP. Under the hood it's a Rust/tokio-driven
chromiumoxide client speaking CDP directly to a bundled chrome-headless-shell.

Typical usage::

    import onyxweb

    # One-shot (uses a shared, process-wide default Client)
    html = onyxweb.fetch("https://example.com")
    png  = onyxweb.screenshot("https://example.com")
    both = onyxweb.fetch_all("https://example.com")

    # Keep a page; read it later with no Chrome and no network
    html.save("page.json")
    page = onyxweb.RenderResult.load("page.json")

    # Explicit Client for batch / tuning
    with onyxweb.Client(concurrency=16) as client:
        for result in client.batch(urls, capture="both"):
            title = result.html.title
            ...

All HTML search (``.dom.query()``, ``.dom.find()``, etc.) runs in Rust for
speed; no Python HTML parsing round-trip.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Final, Literal, Protocol, overload

from pydantic import BaseModel as _BaseModel

from onyxweb._logging import configure as _configure_logging, logger, set_log_level
from onyxweb._onyxweb import (
    ChromeExitedError as ChromeExitedError,
    Client as _RustClient,
    Dom as Dom,
    Element as Element,
    OnyxwebError as OnyxwebError,
    QueueTimeoutError as QueueTimeoutError,
    _FetchOutput,
    _RenderOutput,
)
from onyxweb.config import (
    _FLAT_KWARG_NAMES,
    _FLAT_KWARG_PATHS,
    _TOP_LEVEL_KWARGS,
    ChromeConfig,
    Click,
    ClientConfig,
    EmulationConfig,
    FetchConfig,
    Fill,
    Hover,
    IncludeConfig,
    NetworkConfig,
    ScreenshotConfig,
    ScriptsConfig,
    TimeoutConfig,
    UserAgentBrandVersion,
    UserAgentMetadata,
    ViewportConfig,
    Wait,
)
from onyxweb.download import (
    CHROME_VERSION as CHROME_VERSION,
    OnyxwebDownloadError as OnyxwebDownloadError,
    aensure_chrome as aensure_chrome,
    ensure_chrome as ensure_chrome,
    find_chrome as find_chrome,
)
from onyxweb.records import (
    PAGE_BUCKETS,
    Bucket,
    BucketView,
    Comment,
    Content,
    Form,
    Frame,
    Image,
    JsonLd,
    Link,
    Meta,
    Overview,
    OverviewRow,
    Resources,
    Script,
    Style,
    count_str,
    overview_str,
    size_str,
)

# Configure Python-side logging at import from ONYXWEB_LOG (defaults "warn").
# The Rust side reads the same env var at PyO3 module init.
_configure_logging()

_client_log = logger.getChild("client")

SNAPSHOT_KEY: Final = "onyxweb_snapshot"
SNAPSHOT_VERSION: Final = 1

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
    "ChromeExitedError",
    "OnyxwebError",
    "QueueTimeoutError",
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
    "IncludeConfig",
    "UserAgentBrandVersion",
    "UserAgentMetadata",
    # Logging
    "logger",
    "set_log_level",
    # Chrome install (host-app integration — see onyxweb.download)
    "ensure_chrome",
    "aensure_chrome",
    "find_chrome",
    "OnyxwebDownloadError",
    "CHROME_VERSION",
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
    def pairs(self) -> list[tuple[str, str]]:
        """Every ``(name, value)`` as received, in order, duplicates (Set-Cookie) kept."""
        return list(self._pairs)

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


class RenderResult:
    """A captured page, sorted into buckets.

    The document itself stays in Rust until you ask for it — ``str(r)`` and
    ``r.html`` materialize it, ``"x" in r`` and ``len(r)`` answer without
    copying. Pass ``r.html`` to anything that needs a real ``str`` (``re``,
    BeautifulSoup, ``file.write``).

    Buckets — ``.scripts`` / ``.styles`` / ``.links`` / ``.images`` /
    ``.iframes`` / ``.forms`` / ``.meta`` / ``.comments`` / ``.json_ld`` — are
    lazy: sizing or printing one costs nothing until records are read.

    Snapshots — ``.save(path)`` writes the page and its response to one JSON file,
    ``RenderResult.load(path)`` reads it back with the same buckets, search and text,
    and ``.snapshot()`` returns the same data as a dict. None of them needs Chrome or
    the network.

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
      - ``.dom`` — Rust-side CSS selection (lazy)
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
    _html: str | None

    def __init__(
        self,
        html: str | None = None,
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
    ) -> None:
        """Construct a RenderResult; ``_raw`` is internal (Rust output object).

        `html` is optional: when `_raw` is present the document is pulled from
        Rust on first access instead of being copied in up front.
        """
        self.errors = errors or []
        self.console_messages = console_messages or []
        self.final_url = final_url
        self.status_code = status_code
        self.elapsed_s = elapsed_s
        self.post_load_results = post_load_results or []
        self.anti_bot = anti_bot
        self.headers = (
            headers
            if headers is not None
            else ResponseHeaders([], "", Hashes(md5="", mmh3=0, sha256=""))
        )
        self.metadata = metadata or ResponseMetadata(
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
        self._raw = _raw
        self._dom = None
        self._html = html

    @property
    def html(self) -> str:
        """The captured HTML as a plain ``str``. Copies out of Rust once."""
        if self._html is None:
            self._html = self._raw.html if self._raw is not None else ""
        return self._html

    @property
    def dom(self) -> Dom:
        """Rust-parsed DOM (lazy). First access triggers html5ever parse."""
        dom = self._dom
        if dom is None:
            # A result never seen by a browser (built by hand, or loaded) parses its own html.
            dom = (
                self._raw.make_dom()
                if self._raw is not None
                else Dom(self.html, self.final_url or None)
            )
            object.__setattr__(self, "_dom", dom)
        return dom

    @cached_property
    def _page(self) -> BucketView:
        """Every bucket, unfiltered, over this result's single parse."""
        return BucketView(self.dom.buckets, {})

    @cached_property
    def content(self) -> Content:
        """What lives in the document itself — inline code, comments, forms, meta."""
        return Content(self.dom.buckets)

    @cached_property
    def resources(self) -> Resources:
        """What the document points the browser at — external code, images, links."""
        return Resources(self.dom.buckets)

    @property
    def scripts(self) -> Bucket[Script]:
        """Every ``<script>`` — inline source and the URLs the page pulls in."""
        return self._page._bucket("scripts")

    @property
    def styles(self) -> Bucket[Style]:
        """Every ``<style>`` body and stylesheet ``<link>``."""
        return self._page._bucket("styles")

    @property
    def links(self) -> Bucket[Link]:
        """Every ``<a href>`` — where the page points, not what it loads."""
        return self._page._bucket("links")

    @property
    def images(self) -> Bucket[Image]:
        """Every ``<img>`` whose ``src`` fetches something."""
        return self._page._bucket("images")

    @property
    def iframes(self) -> Bucket[Frame]:
        """Every ``<iframe>`` — ``srcdoc`` bodies, framed documents, and blank frames."""
        return self._page._bucket("iframes")

    @property
    def forms(self) -> Bucket[Form]:
        """Every ``<form>`` and the fields it submits, hidden ones included."""
        return self._page._bucket("forms")

    @property
    def meta(self) -> Bucket[Meta]:
        """Every ``<meta>``; its ``name``, ``property``, ``http-equiv`` or ``charset`` is `name`."""
        return self._page._bucket("meta")

    @property
    def comments(self) -> Bucket[Comment]:
        """Every HTML comment, in document order."""
        return self._page._bucket("comments")

    @property
    def json_ld(self) -> Bucket[JsonLd]:
        """Every ``application/ld+json`` block, decoded; malformed ones skipped."""
        return self._page._bucket("json_ld")

    @property
    def text(self) -> str:
        """What the page displays — script and style source excluded."""
        return self.dom.buckets.text()

    @property
    def title(self) -> str | None:
        """The ``<title>`` text, or ``None`` when the page has none."""
        return self.dom.buckets.title()

    @overload
    def overview(self, prnt: Literal[False] = False) -> Overview: ...

    @overload
    def overview(self, prnt: Literal[True]) -> None: ...

    def overview(self, prnt: bool = False) -> Overview | None:
        """Query/print every bucket's count and size, read from the parse.

        Builds no record, so it stays cheap on a page of any size.

        Args:
            prnt: If True, prints the overview table instead of returning it;
                always returns None.

        Returns:
            `Overview`; None if prnt.
        """
        rows, text_size, total_size = self.dom.buckets.counts()
        overview = Overview([OverviewRow(*row) for row in rows], text_size, total_size)
        if prnt:
            print(overview_str(overview))
            return None
        return overview

    def search(
        self,
        query: str,
        *,
        field: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> dict[str, Bucket[Any]]:
        """Search every bucket at once and keep the ones with a match.

        Args:
            query: Text to find — a substring, or a pattern when `regex` is set.
            field: Search only this record field. Buckets whose records have no
                such field are skipped rather than raising.
            case_sensitive: Match letter case exactly. Off by default.
            regex: Treat `query` as a Rust ``regex`` pattern — linear time, so no
                lookaround or backreferences.

        Returns:
            Bucket name -> lazy bucket of its matches, in overview order; empty
            when nothing matched.

        Raises:
            ValueError: If `query` is not a valid pattern.
        """
        hits: dict[str, Bucket[Any]] = {}
        for name in PAGE_BUCKETS:
            bucket = self._page._bucket(name)
            if field is not None and field not in bucket.fields:
                continue
            found = bucket.search(query, field=field, case_sensitive=case_sensitive, regex=regex)
            if len(found):
                hits[name] = found
        return hits

    def snapshot(self) -> dict[str, Any]:
        """Return everything but a screenshot as a JSON-ready dict, the form `save` writes.

        Keys: ``html``, ``final_url``, ``status_code``, ``elapsed_s``, ``errors``,
        ``console_messages``, ``post_load_results``, ``metadata``, ``headers`` and
        ``anti_bot``, plus an ``onyxweb_snapshot`` version marker. A change an older
        onyxweb could not read bumps that version, and `load` rejects a newer one.
        """
        headers = self.headers
        return {
            SNAPSHOT_KEY: SNAPSHOT_VERSION,
            "html": self.html,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "elapsed_s": self.elapsed_s,
            "errors": self.errors,
            "console_messages": [asdict(m) for m in self.console_messages],
            "post_load_results": self.post_load_results,
            "metadata": asdict(self.metadata),
            "headers": {
                "pairs": [list(pair) for pair in headers.pairs],
                "raw": headers.raw,
                "hashes": asdict(headers.hashes),
            },
            "anti_bot": asdict(self.anti_bot) if self.anti_bot else None,
        }

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write a JSON snapshot that `load` reads back without Chrome or the network.

        Args:
            path: Destination file, overwritten if it exists.
        """
        Path(path).write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> RenderResult:
        """Read a result written by `save`; its buckets, search and text work offline.

        Args:
            path: A file written by `RenderResult.save`.

        Returns:
            A `RenderResult` that reads like the one saved, with no browser behind it.

        Raises:
            ValueError: If the file is not a snapshot, or was written by a newer onyxweb.
        """
        fix = "write one with RenderResult.save()"
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError as ve:
            raise ValueError(f"{path} is not an onyxweb snapshot; {fix}.") from ve
        if not isinstance(data, dict) or SNAPSHOT_KEY not in data:
            raise ValueError(f"{path} is not an onyxweb snapshot; {fix}.")
        if data[SNAPSHOT_KEY] != SNAPSHOT_VERSION:
            raise ValueError(
                f"{path} is snapshot version {data[SNAPSHOT_KEY]}, but this onyxweb reads "
                f"version {SNAPSHOT_VERSION}; upgrade onyxweb, or {fix} again."
            )
        meta = data["metadata"]
        cert = meta["cert_info"]
        head = data["headers"]
        return cls(
            data["html"],
            errors=data["errors"],
            console_messages=[ConsoleMessage(**m) for m in data["console_messages"]],
            final_url=data["final_url"],
            status_code=data["status_code"],
            elapsed_s=data["elapsed_s"],
            post_load_results=data["post_load_results"],
            metadata=ResponseMetadata(
                **{
                    **meta,
                    "body_hashes": Hashes(**meta["body_hashes"]),
                    "redirect_chain": [RedirectHop(**hop) for hop in meta["redirect_chain"]],
                    "cert_info": CertInfo(**cert) if cert else None,
                }
            ),
            headers=ResponseHeaders(
                [(name, value) for name, value in head["pairs"]],
                head["raw"],
                Hashes(**head["hashes"]),
            ),
            anti_bot=AntiBot(**data["anti_bot"]) if data["anti_bot"] else None,
        )

    def __str__(self) -> str:
        return self.html

    def __contains__(self, needle: object) -> bool:
        """Case-sensitive substring search over the whole document, like ``str``."""
        if not isinstance(needle, str):
            return False
        # Scan the capture in Rust unless the HTML is already a Python string.
        if self._html is None and self._raw is not None:
            return self._raw.contains(needle)
        return needle in self.html

    def __len__(self) -> int:
        """Characters of captured HTML, counted in Rust unless already copied out."""
        if self._html is None and self._raw is not None:
            return self._raw.char_len()
        return len(self.html)

    def __repr__(self) -> str:
        overview = self.overview()
        totals: dict[str, int] = {}
        for row in overview.rows:
            totals[row.bucket] = totals.get(row.bucket, 0) + row.count
        nouns = (("scripts", "script"), ("styles", "style"), ("forms", "form"), ("links", "link"))
        parts = [size_str(overview.total_size)]
        parts += [count_str(totals[plural], singular, plural) for plural, singular in nouns]
        parts.append(f"{size_str(overview.text_size)} text")
        if self.errors:
            parts.append(count_str(len(self.errors), "error", "errors"))
        # A data: URL carries the whole page, so the URL is clipped.
        url = self.final_url if len(self.final_url) <= 60 else self.final_url[:59] + "…"
        if url:
            parts.append(repr(url))
        return f"<RenderResult {' · '.join(parts)}>"


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
        # Nothing changed, so leave the pooled tabs (and their cookies) alone.
        if new_data == old_data:
            return
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
                    raise TypeError(
                        f"unknown screenshot kwarg: {k!r}; "
                        f"use one of: {', '.join(sorted(_SCREENSHOT_KWARGS))}"
                    )
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
        ...         print(result.title)
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
        # Nothing changed, so leave the pooled tabs (and their cookies) alone.
        if new_data == old_data:
            return
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
            ``RenderResult`` — the page sorted into buckets, with ``.html``,
            ``.title``, ``.text``, ``.errors``, ``.final_url``, ``.status_code``,
            ``.elapsed_s`` and ``.dom``.

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
                    raise TypeError(
                        f"unknown screenshot kwarg: {k!r}; "
                        f"use one of: {', '.join(sorted(_SCREENSHOT_KWARGS))}"
                    )
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

_FETCH_KWARGS = {
    "actions",
    "block_navigation",
    "block_urls",
    "extra_headers",
    "bypass_anti_bot",
    "hash_navigation",
    "post_load_scripts",
    "scripts",
    "timeout_ms",
    "wait_until",
    "wait_after_ms",
    "wait_after_post_load_ms",
}


def _merge_fetch_config(base: FetchConfig | None, overrides: dict[str, Any]) -> FetchConfig:
    if base is None and not overrides:
        return FetchConfig()
    data: dict[str, Any] = base.model_dump() if base else {}
    for k, v in overrides.items():
        if k not in _FETCH_KWARGS:
            raise TypeError(
                f"unknown fetch kwarg: {k!r}; use one of: {', '.join(sorted(_FETCH_KWARGS))}"
            )
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


def _flat_kwargs_to_partial(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Translate flat kwargs into a sparse nested dict.

    Only mentioned fields appear in the output; defaults are NOT filled in, unlike
    ``ClientConfig.from_flat``. Meant for merging onto an existing config; both
    read the kwarg names from ``onyxweb.config``.
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
        if k in _TOP_LEVEL_KWARGS:
            out[k] = v
            continue
        if k not in _FLAT_KWARG_PATHS:
            raise TypeError(f"unknown ClientConfig kwarg: {k!r}; use one of: {_FLAT_KWARG_NAMES}")
        sub, field = _FLAT_KWARG_PATHS[k]
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
