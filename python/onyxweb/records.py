"""Bucket record shapes and the table formatter that renders them.

A bucket is one category of a captured page — its scripts, its styles. Records
are frozen dataclasses, matching the result records in ``onyxweb.__init__``.
``Bucket`` stays lazy: ``len()`` and ``repr()`` answer from Rust, and nothing
crosses the FFI boundary until a caller indexes or iterates.

Import these by full module path; they are not re-exported from the package
root::

    from onyxweb.records import Bucket, Script
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from inspect import signature
from typing import TYPE_CHECKING, Any, Final, Literal, NoReturn, TypeVar, overload

from onyxweb._onyxweb import Buckets as _RustBuckets

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

PREVIEW_WIDTH: Final[int] = 60
"""Characters per preview cell in a bucket table."""

HEAD_ROWS: Final[int] = 10
"""Rows a bucket shows before it reports the remainder as a count."""

_REPR_WIDTH: Final[int] = 40
"""Characters of body text in a single record's repr."""


def _clip(s: str, width: int = _REPR_WIDTH) -> str:
    """One-line excerpt for a record repr, bounded before it collapses runs."""
    return " ".join(s[: width * 4].split())[:width]


#: Every bucket record is a frozen dataclass, so `asdict` is always valid.
R = TypeVar("R", bound="DataclassInstance")

Where = Literal["inline", "external"]

#: A frame's side — or ``"blank"`` when it holds nothing and loads nothing.
FrameWhere = Literal["inline", "external", "blank"]

#: One search term, as Rust receives it: ``(needle, field, case_sensitive, regex)``.
Query = tuple[str, str | None, bool, bool]


# ----------------------------------------------------------------------------
# Record shapes
# ----------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class Script:
    """One ``<script>`` — inline source, or a URL the document pulls in.

    Attributes:
        where: ``"inline"`` when the source lives in this document,
            ``"external"`` when the document loads it by URL.
        text: The script source; ``None`` for an external script, whose body is
            never present in the DOM.
        url: Absolute URL, resolved against ``<base href>`` or the document URL;
            ``None`` for an inline script.
        raw: The ``src`` attribute exactly as authored; ``None`` when inline.
        type: The ``type`` attribute, or ``""``. An ``application/ld+json``
            block is never a Script — it belongs to the ``json_ld`` bucket.
        attrs: Every attribute on the tag. ``integrity`` and ``nonce`` survive
            here because SRI and CSP are recon signals.
    """

    where: Where
    text: str | None
    url: str | None
    raw: str | None
    type: str
    attrs: dict[str, str]

    def __repr__(self) -> str:
        size = f" {len(self.text)}B" if self.text is not None else ""
        return f"<Script {self.where}{size} {_clip(self.text or self.url or '')!r}>"


@dataclass(frozen=True, repr=False)
class Style:
    """One ``<style>`` body, or a stylesheet ``<link>`` the document pulls in.

    Attributes:
        where: ``"inline"`` for a ``<style>`` body, ``"external"`` for a
            stylesheet ``<link>``.
        text: The CSS source; ``None`` when external.
        url: Absolute URL, resolved against ``<base href>`` or the document URL;
            ``None`` when inline.
        raw: The ``href`` attribute exactly as authored; ``None`` when inline.
        media: The ``media`` attribute, or ``None``.
    """

    where: Where
    text: str | None
    url: str | None
    raw: str | None
    media: str | None

    def __repr__(self) -> str:
        size = f" {len(self.text)}B" if self.text is not None else ""
        return f"<Style {self.where}{size} {_clip(self.text or self.url or '')!r}>"


@dataclass(frozen=True, repr=False)
class Link:
    """One ``<a href>`` — a target the page points at but never fetches.

    Attributes:
        url: Absolute URL, resolved against ``<base href>`` or the document URL.
        raw: The ``href`` exactly as authored.
        text: The anchor's visible text.
    """

    url: str
    raw: str
    text: str

    def __repr__(self) -> str:
        return f"<Link {_clip(self.url)!r} {_clip(self.text, 24)!r}>"


@dataclass(frozen=True, repr=False)
class Image:
    """One ``<img>`` whose ``src`` fetches something — an empty or ``about:`` one doesn't.

    Attributes:
        url: Absolute URL. A ``data:`` URI resolves to itself, so it equals `raw`.
        raw: The ``src`` exactly as authored.
        alt: The ``alt`` text, or ``""``.
    """

    url: str
    raw: str
    alt: str

    def __repr__(self) -> str:
        return f"<Image {_clip(self.url)!r} {_clip(self.alt, 24)!r}>"


@dataclass(frozen=True, repr=False)
class Frame:
    """One ``<iframe>`` — an inline ``srcdoc`` body, a document pulled in, or a blank frame.

    ``srcdoc`` wins over ``src``: the browser renders the body and never requests
    the ``src``. Such a frame is ``"inline"``, but keeps its unused ``src`` in
    `url` / `raw` because a declared-but-dead address is a recon signal. A frame
    with neither a body nor a ``src`` that fetches is ``"blank"`` — often an ad
    slot a script fills — and belongs to neither ``content`` nor ``resources``.

    Attributes:
        where: ``"external"`` only when the frame loads its ``src``;
            ``"blank"`` when it has no ``src``, an empty one, or an ``about:`` one.
        srcdoc: The inline document body, or ``None``.
        url: Absolute URL of the ``src``, or ``None`` when there is none. A blank
            frame's ``src`` is kept as authored rather than resolved.
        raw: The ``src`` exactly as authored, or ``None``.
    """

    where: FrameWhere
    srcdoc: str | None
    url: str | None
    raw: str | None

    def __repr__(self) -> str:
        return f"<Frame {self.where} {_clip(self.url or self.srcdoc or '')!r}>"


@dataclass(frozen=True)
class Input:
    """One field a form submits.

    Attributes:
        name: The ``name`` attribute, or ``""``.
        type: The ``type`` attribute, or ``""`` — ``"hidden"`` included.
        value: The ``value`` attribute, or ``""``.
    """

    name: str
    type: str
    value: str


@dataclass(frozen=True, repr=False)
class Form:
    """One ``<form>`` and the fields it submits, hidden ones included.

    Attributes:
        url: Absolute submit target, resolved like any other URL.
        raw: The ``action`` exactly as authored.
        method: Lowercased ``method``; ``"get"`` when the page omits it, matching
            the HTML default.
        inputs: Every ``input`` / ``select`` / ``textarea`` inside the form.
    """

    url: str
    raw: str
    method: str
    inputs: list[Input]

    @classmethod
    def _from_row(cls, url: str, raw: str, method: str, inputs: list[dict[str, str]]) -> Form:
        """Build from a Rust row, promoting nested field dicts to `Input`."""
        return cls(url=url, raw=raw, method=method, inputs=[Input(**i) for i in inputs])

    def __repr__(self) -> str:
        fields = count_str(len(self.inputs), "field", "fields")
        return f"<Form {self.method} {_clip(self.url)!r} {fields}>"


@dataclass(frozen=True, repr=False)
class Meta:
    """One ``<meta>``.

    Attributes:
        name: The ``name``, ``property`` or ``http-equiv`` value — all three fold
            here so a caller looks in one place. ``"charset"`` for a
            ``<meta charset>``; ``""`` when nothing names the tag.
        content: The ``content`` attribute — the encoding for a
            ``<meta charset>`` — or ``""``.
    """

    name: str
    content: str

    def __repr__(self) -> str:
        return f"<Meta {self.name}={_clip(self.content)!r}>"


@dataclass(frozen=True, repr=False)
class Comment:
    """One HTML comment.

    Attributes:
        text: The comment body, delimiters excluded.
    """

    text: str

    def __repr__(self) -> str:
        return f"<Comment {_clip(self.text)!r}>"


@dataclass(frozen=True, repr=False)
class Resource:
    """One subresource the browser fetches on the page's behalf.

    Attributes:
        kind: ``"script"``, ``"style"``, ``"image"`` or ``"iframe"``.
        url: Absolute URL, resolved like any other.
        raw: The ``src`` / ``href`` exactly as authored.
    """

    kind: Literal["script", "style", "image", "iframe"]
    url: str
    raw: str

    def __repr__(self) -> str:
        return f"<Resource {self.kind} {_clip(self.url)!r}>"


@dataclass(frozen=True, repr=False)
class JsonLd:
    """One ``application/ld+json`` block, decoded in Rust.

    A malformed block never reaches here — it is skipped during extraction.

    Attributes:
        data: The decoded JSON, as Python natives.
    """

    data: Any

    def __repr__(self) -> str:
        return f"<JsonLd {_clip(str(self.data))!r}>"


@dataclass(frozen=True, repr=False)
class Match:
    """One place a query matched inside a bucket's records.

    Attributes:
        index: The record's position in the bucket searched, so
            ``bucket[index]`` and ``bucket.text(index)`` reach it.
        field: The record field holding the match — ``"text"``, ``"url"``, or one
            with nested values such as ``"attrs"``.
        text: The matched text, in the page's own letter case.
        groups: Each capture group's text; ``None`` for a group that took no part.
        start: Character offset of the match in the string it came from — the
            field's own value, for a string field.
        end: Character offset just past the match.
    """

    index: int
    field: str
    text: str
    groups: tuple[str | None, ...]
    start: int
    end: int

    @property
    def value(self) -> str:
        """The first capture group when it took part, else the whole match."""
        first = self.groups[0] if self.groups else None
        return self.text if first is None else first

    def __repr__(self) -> str:
        return f"<Match #{self.index} {self.field} {_clip(self.value)!r}>"


# ----------------------------------------------------------------------------
# Formatter — returns the table; callers print it
# ----------------------------------------------------------------------------


def count_str(n: int, singular: str, plural: str) -> str:
    """Render a count with a noun that agrees. Ex: ``(1, "form", "forms")`` -> ``"1 form"``."""
    return f"{n} {singular if n == 1 else plural}"


def size_str(n: int | None) -> str:
    """Render a byte count at a readable scale; ``—`` when there are no bytes to count.

    Ex: ``412`` -> ``"412 B"``, ``3_100`` -> ``"3.1 KB"``, ``1_200_000`` -> ``"1.2 MB"``.
    """
    if n is None:
        return "—"
    for unit, scale in (("MB", 1_000_000), ("KB", 1_000)):
        if n >= scale:
            value = n / scale
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{n} B"


def bucket_str(
    header: str,
    total: int,
    rows: list[dict[str, Any]],
    column: str | None,
    preview: str = "preview",
) -> str:
    """Render a bucket's head rows as a table under `header`.

    Args:
        header: Title line, e.g. ``"Scripts · 4 (2 inline, 2 external)"``.
        total: How many records the bucket holds in full.
        rows: ``{index, where, size, preview, count}`` dicts from the Rust
            ``head`` or ``matches`` call.
        column: Title of the leading column — ``"where"`` or ``"kind"`` — or
            ``None`` to leave it out.
        preview: Title of the last column.

    Returns:
        The table, title line first. The size column appears only when some row
        carries bytes, and a ``×`` column only when a row stands for repeats; a
        trailing line counts the records the head left out.
    """
    show_size = any(row["size"] is not None for row in rows)
    show_count = any(row["count"] > 1 for row in rows)
    head = [f"{'#':>3}"] + ([f"{column:<8}"] if column else [])
    head += [f"{'size':>7}"] if show_size else []
    head += [f"{'×':>4}"] if show_count else []
    lines = [header, "  ".join([*head, preview])]
    for row in rows:
        cells = [f"{row['index']:>3}"]
        if column:
            cells.append(f"{row['where'] or '':<8}")
        if show_size:
            cells.append(f"{size_str(row['size']):>7}")
        if show_count:
            cells.append(f"{row['count'] if row['count'] > 1 else '':>4}")
        cells.append(str(row["preview"] or ""))
        lines.append("  ".join(cells))
    if (shown := sum(row["count"] for row in rows)) < total:
        lines.append(f"… {total - shown} more · [i] for one · .search(q) to filter")
    return "\n".join(lines)


# Not a NamedTuple: tuple's own count() method would shadow the `count` field.
@dataclass(frozen=True)
class OverviewRow:
    """One line of an `Overview` — a bucket, or one side of a split bucket."""

    bucket: str
    where: FrameWhere | None
    count: int
    size: int | None  # bytes in the document; None when the bytes aren't in it


@dataclass(frozen=True, repr=False)
class Overview:
    """Every bucket's count and size, read from the parse without building a record.

    Its ``repr`` is the table, so evaluating ``r.overview()`` in a REPL shows it.

    Attributes:
        rows: One `OverviewRow` per bucket; split buckets appear once per side.
        text_size: Bytes of visible text — what ``RenderResult.text`` returns.
        total_size: Bytes of the whole captured document.
    """

    rows: list[OverviewRow]
    text_size: int
    total_size: int

    def __repr__(self) -> str:
        return overview_str(self)


def overview_str(overview: Overview) -> str:
    """Render an `Overview` as a table, one line per bucket and side.

    Args:
        overview: Counts and sizes from ``RenderResult.overview()``.

    Returns:
        The table, with visible text and the document total beneath the buckets.
    """
    rule = "─" * 38
    lines = [f"{'bucket':<9} {'where':<9} {'n':>6}  {'size':>8}", rule]
    previous = ""
    for row in overview.rows:
        name = "" if row.bucket == previous else row.bucket
        previous = row.bucket
        lines.append(f"{name:<9} {row.where or '':<9} {row.count:>6}  {size_str(row.size):>8}")
    lines.append(f"{'text':<9} {'':<9} {'':>6}  {size_str(overview.text_size):>8}")
    lines.append(rule)
    lines.append(f"{'total':<9} {'':<9} {'':>6}  {size_str(overview.total_size):>8}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Bucket
# ----------------------------------------------------------------------------


class Bucket(Sequence[R]):
    """One category of the page, materialized only when records are read.

    ``len()`` and ``repr()`` answer from Rust without building records, so
    sizing or previewing a bucket on a large page costs nothing. Indexing or
    iterating pulls every record across once and caches it.
    """

    __slots__ = (
        "_rust",
        "_name",
        "_label",
        "_factory",
        "_column",
        "_body",
        "_where",
        "_queries",
        "_records",
    )

    _rust: _RustBuckets
    _name: str
    _label: str
    _factory: Callable[..., R]
    _column: str | None
    _body: tuple[str, ...]
    _where: Where | None
    _queries: tuple[Query, ...]
    _records: list[R] | None

    def __init__(
        self,
        rust: _RustBuckets,
        name: str,
        label: str,
        factory: Callable[..., R],
        *,
        column: str | None = None,
        body: tuple[str, ...] = (),
        where: Where | None = None,
        queries: tuple[Query, ...] = (),
    ) -> None:
        self._rust = rust
        self._name = name
        self._label = label
        self._factory = factory
        self._column = column
        self._body = body
        self._where = where
        self._queries = queries
        self._records = None

    def _materialize(self) -> list[R]:
        """Build and cache every record. The one place FFI cost is paid."""
        if self._records is None:
            rows = self._rust.records(self._name, self._where, list(self._queries))
            self._records = [self._factory(**row) for row in rows]
        return self._records

    def __len__(self) -> int:
        return self._rust.count(self._name, self._where, list(self._queries))

    @overload
    def __getitem__(self, index: int) -> R: ...

    @overload
    def __getitem__(self, index: slice) -> list[R]: ...

    def __getitem__(self, index: int | slice) -> R | list[R]:
        return self._materialize()[index]

    def __iter__(self) -> Iterator[R]:
        return iter(self._materialize())

    def __repr__(self) -> str:
        return self.table()

    @property
    def fields(self) -> tuple[str, ...]:
        """Field names of this bucket's records — what ``search(field=...)`` accepts."""
        # A factory's parameters are the row keys Rust sends: the record's fields.
        return tuple(signature(self._factory).parameters)

    def search(
        self,
        query: str,
        *,
        field: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
    ) -> Bucket[R]:
        """Keep the records containing `query`, matched in Rust.

        Without `field` every string in a record is searched — bodies, URLs,
        attribute names and values, nested form fields, JSON-LD keys and values —
        except ``where`` and ``kind``, which are onyxweb's labels rather than page
        bytes. The result is itself lazy, and searching it again narrows it.

        Args:
            query: Text to find — a substring, or a pattern when `regex` is set.
            field: Search only this record field; see `fields`.
            case_sensitive: Match letter case exactly. Off by default.
            regex: Treat `query` as a Rust ``regex`` pattern — linear time, so no
                lookaround or backreferences.

        Returns:
            A lazy bucket of the matching records.

        Raises:
            ValueError: If `field` is not a field of these records. An invalid
                pattern raises ValueError when the result is first read.
        """
        if field is not None and field not in self.fields:
            raise ValueError(
                f"{self._label} records have no field {field!r}; "
                f"search one of {', '.join(self.fields)}."
            )
        term: Query = (query, field, case_sensitive, regex)
        return Bucket(
            self._rust,
            self._name,
            self._label,
            self._factory,
            column=self._column,
            body=self._body,
            where=self._where,
            queries=(*self._queries, term),
        )

    @overload
    def matches(
        self,
        query: str,
        *,
        field: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        prnt: Literal[False] = False,
    ) -> list[Match]: ...

    @overload
    def matches(
        self,
        query: str,
        *,
        field: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        prnt: Literal[True],
    ) -> None: ...

    def matches(
        self,
        query: str,
        *,
        field: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        prnt: bool = False,
    ) -> list[Match] | None:
        """Query/print every place `query` matches in this bucket's records.

        Where `search` keeps whole records, this hands back the matched text, so a
        key inside a 700 KB script comes back without the script. It reads the
        strings `search` reads, in record order; a text that an earlier field of
        one record already matched counts once — a URL sits in ``url``, ``raw``
        and ``attrs`` alike. Matching runs in Rust.

        Args:
            query: Text to find — a substring, or a pattern when `regex` is set.
            field: Match only inside this record field; see `fields`.
            case_sensitive: Match letter case exactly. Off by default.
            regex: Treat `query` as a Rust ``regex`` pattern; its first capture
                group becomes each match's `Match.value`.
            prnt: If True, prints every match framed by its surroundings instead
                of returning them; always returns None.

        Returns:
            Every `Match`, in record order. None if prnt.

        Raises:
            ValueError: If `field` is not a field of these records, or `query` is
                not a valid pattern.
        """
        if field is not None and field not in self.fields:
            raise ValueError(
                f"{self._label} records have no field {field!r}; "
                f"search one of {', '.join(self.fields)}."
            )
        term: Query = (query, field, case_sensitive, regex)
        queries = list(self._queries)
        if not prnt:
            rows = self._rust.matches(self._name, term, self._where, queries)
            return [Match(**row) for row in rows]
        rows = self._rust.matches(self._name, term, self._where, queries, PREVIEW_WIDTH)
        side = f" {self._where}" if self._where else ""
        found = count_str(len(rows), "match", "matches")
        hit_records = len({row["index"] for row in rows})
        header = f"{self._label} · {found} in {hit_records} of {len(self)}{side}"
        column = None if self._where else self._column
        print(bucket_str(header, len(rows), rows, column, "match") if rows else header)
        return None

    @overload
    def text(self, index: int, *, prnt: Literal[False] = False) -> str: ...

    @overload
    def text(self, index: int, *, prnt: Literal[True]) -> None: ...

    def text(self, index: int, *, prnt: bool = False) -> str | None:
        """Query/print one record's whole content given its index.

        The body when the record has one — script or style source, a frame's
        ``srcdoc``, a comment, JSON-LD as indented JSON — else its URL, or a
        meta tag's ``content``. Nothing is clipped, unlike a record's ``repr``.

        Args:
            index: Position in this bucket, as a table's ``#`` column or
                `Match.index` gives it.
            prnt: If True, prints the content instead of returning it; always
                returns None.

        Returns:
            The content; ``""`` when the record has none. None if prnt.

        Raises:
            IndexError: If `index` is past the end of the bucket.
        """
        record = self[index]
        # The first body field the record fills wins: a script's source, else its URL.
        found = next((v for name in self._body if (v := getattr(record, name)) is not None), "")
        content = (
            found if isinstance(found, str) else json.dumps(found, indent=2, ensure_ascii=False)
        )
        if prnt:
            print(content)
            return None
        return content

    def table(self, n: int = HEAD_ROWS, width: int = PREVIEW_WIDTH) -> str:
        """Render the first `n` records as a table, previews clipped to `width`."""
        total = len(self)
        side = f" {self._where}" if self._where else ""
        if self._queries:

            def term_str(term: Query) -> str:
                needle, field, _, regex = term
                shown = f"/{needle}/" if regex else repr(needle)
                return f"{field}:{shown}" if field else shown

            unsearched = self._rust.count(self._name, self._where, [])
            terms = ", ".join(term_str(q) for q in self._queries)
            header = f"{self._label} · {total} of {unsearched}{side} matching {terms}"
            if total == 0:
                return header
        elif total == 0:
            return f"{self._label} · empty"
        elif self._where is not None:
            header = f"{self._label} · {total}{side}"
        elif self._column == "where":
            inline = self._rust.count(self._name, "inline")
            external = self._rust.count(self._name, "external")
            # Only frames can be blank, so the count shows only when one is.
            blank = f", {total - inline - external} blank" if total > inline + external else ""
            header = f"{self._label} · {total} ({inline} inline, {external} external{blank})"
        else:
            header = f"{self._label} · {total}"
        rows = self._rust.head(self._name, n, width, self._where, list(self._queries))
        # A filtered bucket's side is in the header, so its column would be constant.
        return bucket_str(header, total, rows, None if self._where else self._column)

    def asdict(self) -> list[dict[str, Any]]:
        """Every record as a plain dict, for JSON pipelines."""
        return [asdict(r) for r in self._materialize()]


# ----------------------------------------------------------------------------
# Views — which buckets a handle holds, and which side of each it keeps
# ----------------------------------------------------------------------------

BUCKET_SPECS: Final[dict[str, tuple[str, Callable[..., Any], str | None, tuple[str, ...]]]] = {
    "scripts": ("Scripts", Script, "where", ("text", "url")),
    "styles": ("Styles", Style, "where", ("text", "url")),
    "links": ("Links", Link, None, ("url",)),
    "images": ("Images", Image, None, ("url",)),
    "iframes": ("Iframes", Frame, "where", ("srcdoc", "url")),
    "forms": ("Forms", Form._from_row, None, ("url",)),
    "meta": ("Meta", Meta, None, ("content",)),
    "comments": ("Comments", Comment, None, ("text",)),
    "json_ld": ("JsonLd", JsonLd, None, ("data",)),
    "loaded": ("Loaded", Resource, "kind", ("url",)),
}
"""Bucket name -> (display label, record factory, leading table column, body
fields). A ``"where"`` column marks a bucket that splits inline/external; the
body fields, first filled one winning, are what ``Bucket.text`` returns. A new
category is one row here plus one Rust extractor."""

PAGE_BUCKETS: Final[tuple[str, ...]] = (
    "scripts",
    "styles",
    "iframes",
    "comments",
    "forms",
    "meta",
    "json_ld",
    "links",
    "images",
)
"""Buckets over the page itself, in overview order. ``loaded`` is left out: it
re-lists scripts, styles, images and frames."""

_CONTENT_SIDES: Final[Mapping[str, Where | None]] = {
    "scripts": "inline",
    "styles": "inline",
    "iframes": "inline",
    "comments": None,
    "forms": None,
    "meta": None,
    "json_ld": None,
}
"""Buckets `Content` holds; a side keeps one half of a split bucket, ``None`` all."""

_RESOURCE_SIDES: Final[Mapping[str, Where | None]] = {
    "scripts": "external",
    "styles": "external",
    "iframes": "external",
    "images": None,
    "links": None,
}
"""Buckets `Resources` holds; same shape as `_CONTENT_SIDES`."""


class BucketView:
    """Lazy, cached buckets over one parsed page, each held to a side.

    Result-level buckets (``r.scripts``) and both views (``r.content``,
    ``r.resources``) are a BucketView; they differ only in the side map. A name
    missing from the map gets the whole, unfiltered bucket.
    """

    __slots__ = ("_rust", "_sides", "_cache")

    _rust: _RustBuckets
    _sides: Mapping[str, Where | None]
    _cache: dict[str, Bucket[Any]]

    def __init__(self, rust: _RustBuckets, sides: Mapping[str, Where | None]) -> None:
        self._rust = rust
        self._sides = sides
        self._cache = {}

    def _bucket(self, name: str) -> Bucket[Any]:
        """Return the cached bucket for `name`, held to this view's side of it."""
        cached = self._cache.get(name)
        if cached is None:
            label, factory, column, body = BUCKET_SPECS[name]
            cached = Bucket(
                self._rust,
                name,
                label,
                factory,
                column=column,
                body=body,
                where=self._sides.get(name),
            )
            self._cache[name] = cached
        return cached


class Content(BucketView):
    """What lives in the document itself.

    The inline halves of scripts, styles and iframes, plus the buckets that only
    ever live in the document — comments, forms, meta and JSON-LD.
    """

    __slots__ = ()

    def __init__(self, rust: _RustBuckets) -> None:
        super().__init__(rust, _CONTENT_SIDES)

    if not TYPE_CHECKING:
        # Runtime-only so mypy still flags a wrong-side access statically.
        def __getattr__(self, name: str) -> NoReturn:
            if name in _RESOURCE_SIDES:
                raise AttributeError(f"Content holds no `{name}` bucket; use `r.resources.{name}`.")
            raise AttributeError(f"'Content' object has no attribute {name!r}")

    @property
    def scripts(self) -> Bucket[Script]:
        """Inline ``<script>`` source."""
        return self._bucket("scripts")

    @property
    def styles(self) -> Bucket[Style]:
        """Inline ``<style>`` bodies."""
        return self._bucket("styles")

    @property
    def iframes(self) -> Bucket[Frame]:
        """Frames whose document lives in a ``srcdoc`` body."""
        return self._bucket("iframes")

    @property
    def comments(self) -> Bucket[Comment]:
        """HTML comments."""
        return self._bucket("comments")

    @property
    def forms(self) -> Bucket[Form]:
        """Forms and the fields they submit."""
        return self._bucket("forms")

    @property
    def meta(self) -> Bucket[Meta]:
        """``<meta>`` declarations."""
        return self._bucket("meta")

    @property
    def json_ld(self) -> Bucket[JsonLd]:
        """Decoded ``application/ld+json`` blocks."""
        return self._bucket("json_ld")


class Resources(BucketView):
    """What the document points the browser at.

    The external halves of scripts, styles and iframes, plus images and links.
    `all` flattens everything actually fetched into one inventory.
    """

    __slots__ = ()

    def __init__(self, rust: _RustBuckets) -> None:
        super().__init__(rust, _RESOURCE_SIDES)

    if not TYPE_CHECKING:
        # Runtime-only so mypy still flags a wrong-side access statically.
        def __getattr__(self, name: str) -> NoReturn:
            if name in _CONTENT_SIDES:
                raise AttributeError(f"Resources holds no `{name}` bucket; use `r.content.{name}`.")
            raise AttributeError(f"'Resources' object has no attribute {name!r}")

    @property
    def scripts(self) -> Bucket[Script]:
        """External scripts, by URL."""
        return self._bucket("scripts")

    @property
    def styles(self) -> Bucket[Style]:
        """Stylesheet ``<link>`` targets."""
        return self._bucket("styles")

    @property
    def iframes(self) -> Bucket[Frame]:
        """Frames that load their ``src``."""
        return self._bucket("iframes")

    @property
    def images(self) -> Bucket[Image]:
        """``<img src>`` targets."""
        return self._bucket("images")

    @property
    def links(self) -> Bucket[Link]:
        """``<a href>`` targets — referenced, never fetched."""
        return self._bucket("links")

    def all(self) -> Bucket[Resource]:
        """Everything the browser fetches, in document order.

        External scripts, stylesheets, images and frames. Links are referenced
        rather than fetched, so they are left out.
        """
        return self._bucket("loaded")


__all__ = [
    "BUCKET_SPECS",
    "HEAD_ROWS",
    "PREVIEW_WIDTH",
    "Bucket",
    "BucketView",
    "Content",
    "Comment",
    "Form",
    "Frame",
    "FrameWhere",
    "Image",
    "Input",
    "JsonLd",
    "Link",
    "Match",
    "Meta",
    "Overview",
    "OverviewRow",
    "PAGE_BUCKETS",
    "Query",
    "Resource",
    "Resources",
    "Script",
    "Style",
    "Where",
    "bucket_str",
    "count_str",
    "overview_str",
    "size_str",
]
