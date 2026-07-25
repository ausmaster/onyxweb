"""Type stubs for the compiled Rust extension ``onyxweb._onyxweb``.

Hand-maintained to mirror the ``#[pymethods]`` / ``#[pyo3(get)]`` surface in
``src/*.rs`` so ``.dom`` queries (Dom/Element) and raw results are fully typed
for downstream consumers.
"""

from collections.abc import Awaitable
from typing import Any, Literal

_CertInfo = tuple[str, list[str], str, float, float]
_AntiBot = tuple[str | None, Literal["challenge", "block"], bool]
_RedirectHop = tuple[str, int, str | None]

class OnyxwebError(RuntimeError):
    url: str  # injected on fetch/batch failures (error.rs into_py_err)
    kind: str

class Element:
    tag: str
    text: str
    html: str
    inner_html: str
    @property
    def attrs(self) -> dict[str, str]: ...
    def attr(self, name: str) -> str | None: ...
    def query(self, selector: str) -> list[Element]: ...
    def query_one(self, selector: str) -> Element | None: ...
    def find(
        self,
        tag: str | None = ...,
        *,
        class_: str | None = ...,
        id: str | None = ...,
        **attrs: str,
    ) -> Element | None: ...
    def find_all(
        self,
        tag: str | None = ...,
        *,
        class_: str | None = ...,
        id: str | None = ...,
        **attrs: str,
    ) -> list[Element]: ...

class Dom:
    def query(self, selector: str) -> list[Element]: ...
    def query_one(self, selector: str) -> Element | None: ...
    def count(self, selector: str) -> int: ...
    def exists(self, selector: str) -> bool: ...
    def select(self, selector: str) -> list[Element]: ...
    def select_one(self, selector: str) -> Element | None: ...
    def find(
        self,
        tag: str | None = ...,
        *,
        class_: str | None = ...,
        id: str | None = ...,
        **attrs: str,
    ) -> Element | None: ...
    def find_all(
        self,
        tag: str | None = ...,
        *,
        class_: str | None = ...,
        id: str | None = ...,
        limit: int | None = ...,
        **attrs: str,
    ) -> list[Element]: ...
    def text(self) -> str: ...
    def html(self) -> str: ...
    def contains(self, needle: str, *, case_sensitive: bool = ...) -> bool: ...
    def find_all_text(self, needle: str, *, case_sensitive: bool = ...) -> list[int]: ...
    def links(self) -> list[str]: ...
    def images(self) -> list[str]: ...
    def title(self) -> str | None: ...

class _ConsoleMessage:
    type: Literal["log", "info", "warning", "error", "debug", "trace"]
    text: str
    timestamp: float

class _RenderOutput:
    html: str
    console_messages: list[_ConsoleMessage]
    final_url: str
    request_url: str
    redirect_chain: list[_RedirectHop]
    status_code: int
    status_text: str
    mime_type: str
    protocol: str
    remote_ip: str | None
    remote_port: int | None
    headers: list[tuple[str, str]]
    header_raw: str
    body_md5: str
    body_mmh3: int
    body_sha256: str
    header_md5: str
    header_mmh3: int
    header_sha256: str
    cert_info: _CertInfo | None
    content_length: int
    elapsed_s: float
    post_load_results: list[str | None]
    anti_bot: _AntiBot | None
    def make_dom(self) -> Dom: ...

class _FetchOutput:
    html: str
    png: bytes
    console_messages: list[_ConsoleMessage]
    final_url: str
    request_url: str
    redirect_chain: list[_RedirectHop]
    status_code: int
    status_text: str
    mime_type: str
    protocol: str
    remote_ip: str | None
    remote_port: int | None
    headers: list[tuple[str, str]]
    header_raw: str
    body_md5: str
    body_mmh3: int
    body_sha256: str
    header_md5: str
    header_mmh3: int
    header_sha256: str
    cert_info: _CertInfo | None
    content_length: int
    elapsed_s: float
    post_load_results: list[str | None]
    anti_bot: _AntiBot | None
    def make_dom(self) -> Dom: ...

class Client:
    def __init__(self, config: dict[str, Any]) -> None: ...
    def fetch(self, url: str, per_call: dict[str, Any]) -> _RenderOutput: ...
    def screenshot(self, url: str, per_shot: dict[str, Any]) -> bytes: ...
    def fetch_all(
        self, url: str, per_call: dict[str, Any], per_shot: dict[str, Any]
    ) -> _FetchOutput: ...
    def batch(self, urls: list[str], capture: str, per_call: dict[str, Any]) -> list[Any]: ...
    def update_config(self, config: dict[str, Any]) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Client: ...
    def __exit__(self, *exc: object) -> None: ...
    def fetch_async(self, url: str, per_call: dict[str, Any]) -> Awaitable[_RenderOutput]: ...
    def screenshot_async(self, url: str, per_shot: dict[str, Any]) -> Awaitable[bytes]: ...
    def fetch_all_async(
        self, url: str, per_call: dict[str, Any], per_shot: dict[str, Any]
    ) -> Awaitable[_FetchOutput]: ...
    def batch_async(
        self, urls: list[str], capture: str, per_call: dict[str, Any]
    ) -> Awaitable[list[Any]]: ...
    def close_async(self) -> Awaitable[None]: ...

def _set_rust_log_level(level: str) -> None: ...
