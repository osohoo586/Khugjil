"""MIME parsing, shape checks (check d) and body normalization to rows of cells.

The HTML normalizer is a stdlib ``HTMLParser`` subclass. It keeps only text: scripts, styles and
all attributes (so every URL) are ignored, nothing is ever fetched, and node/row counts are capped.
"""

from __future__ import annotations

import email.policy
import re
from dataclasses import dataclass
from email.message import EmailMessage, Message
from email.parser import BytesParser
from html.parser import HTMLParser

from khugjil.crypto import sha256_hex
from khugjil.ingest.reasons import Reason
from khugjil.ingest.sanitize import clean_text
from khugjil.settings import MAX_MESSAGE_BYTES

MAX_MIME_DEPTH = 5
MAX_HTML_NODES = 20_000
MAX_ROWS = 2_000
_ALLOWED_LEAF_TYPES = frozenset({"text/plain", "text/html"})
# Banks attach their logo (Golomt does). With the owner's approval (PLAN Q8, 2026-11-11) such
# raster images are skipped: never decoded, never read. SVG (scriptable) and every other
# non-text part still count as attachments and drop the email.
_SKIPPED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif"})
_ALLOWED_MULTIPART = frozenset({"multipart/alternative", "multipart/mixed", "multipart/related"})

Rows = list[list[str]]


class MimeError(Exception):
    pass


def parse_message(raw: bytes) -> EmailMessage:
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    if not isinstance(msg, EmailMessage):  # pragma: no cover - policy.default guarantees this
        raise MimeError("unexpected message class")
    return msg


def message_id_hash(msg: Message) -> str:
    try:
        value = str(msg.get("Message-ID", "") or "")
    except Exception:  # pragma: no cover - defensive: malformed header
        value = ""
    return sha256_hex(value.strip())


@dataclass(frozen=True)
class Bodies:
    html: str | None
    plain: str | None


def _leaves(part: Message, depth: int) -> list[Message] | Reason:
    if depth > MAX_MIME_DEPTH:
        return Reason.MIME_INVALID
    ctype = part.get_content_type()
    if part.is_multipart():
        if ctype not in _ALLOWED_MULTIPART:
            return Reason.ATTACHMENT
        out: list[Message] = []
        payload = part.get_payload()
        if not isinstance(payload, list):  # pragma: no cover - defensive
            return Reason.MIME_INVALID
        for sub in payload:
            if not isinstance(sub, Message):  # pragma: no cover - defensive
                return Reason.MIME_INVALID
            got = _leaves(sub, depth + 1)
            if isinstance(got, Reason):
                return got
            out.extend(got)
        return out
    if ctype in _SKIPPED_IMAGE_TYPES:
        return []
    if part.get_filename() is not None or part.get_content_disposition() == "attachment":
        return Reason.ATTACHMENT
    if ctype not in _ALLOWED_LEAF_TYPES:
        return Reason.ATTACHMENT
    return [part]


def inspect_bodies(msg: EmailMessage, raw_len: int) -> Bodies | Reason:
    """Check d: only text leaves (logo images skipped unread), size limits, bounded nesting.
    Returns decoded bodies."""
    if raw_len > MAX_MESSAGE_BYTES:
        return Reason.TOO_LARGE
    leaves = _leaves(msg, 0)
    if isinstance(leaves, Reason):
        return leaves
    html: str | None = None
    plain: str | None = None
    total = 0
    for leaf in leaves:
        try:
            content = leaf.get_content()  # type: ignore[attr-defined]
        except (LookupError, UnicodeError, ValueError, KeyError):
            return Reason.MIME_INVALID
        if not isinstance(content, str):  # pragma: no cover - text/* is always str
            return Reason.MIME_INVALID
        total += len(content.encode("utf-8"))
        if total > MAX_MESSAGE_BYTES:
            return Reason.TOO_LARGE
        if leaf.get_content_type() == "text/html" and html is None:
            html = content
        elif leaf.get_content_type() == "text/plain" and plain is None:
            plain = content
    if html is None and plain is None:
        return Reason.MIME_INVALID
    return Bodies(html=html, plain=plain)


class _TooComplexError(Exception):
    pass


class _TableText(HTMLParser):
    _SKIP = frozenset({"script", "style", "head", "title", "noscript", "template", "svg"})
    _BLOCK = frozenset(
        {
            "p", "div", "br", "li", "ul", "ol", "table", "tbody", "thead", "tfoot", "section",
            "article", "header", "footer", "center", "blockquote", "hr", "h1", "h2", "h3", "h4",
            "h5", "h6", "body", "html",
        }
    )  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: Rows = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._loose: list[str] = []
        self._skip = 0
        self._nodes = 0

    def _flush_loose(self) -> None:
        if self._loose:
            self.rows.append(["".join(self._loose)])
            self._loose = []

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._nodes += 1
        if self._nodes > MAX_HTML_NODES or len(self.rows) > MAX_ROWS:
            raise _TooComplexError
        if tag in self._SKIP:
            self._skip += 1
        elif self._skip:
            return
        elif tag == "tr":
            self._flush_loose()
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            self._flush_loose()
            if self._row is None:
                self._row = []
            self._close_cell()
            self._cell = []
        elif tag in self._BLOCK:
            if self._cell is not None:
                self._cell.append(" ")
            else:
                self._flush_loose()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr" or tag == "table":
            self._close_row()
        elif tag in self._BLOCK and self._cell is None:
            self._flush_loose()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._row is None:
            self._loose.append(data)
        elif data.strip():
            # Text between cells (malformed HTML) is never dropped: dropping it could hide
            # deny-listed words from check e.
            self._close_row()
            self._loose.append(data)

    def finish(self) -> Rows:
        self.close()
        self._close_row()
        self._flush_loose()
        return self.rows


def _normalize(rows: Rows) -> Rows:
    out: Rows = []
    for row in rows:
        cells = [c for c in (clean_text(cell) for cell in row) if c]
        if cells:
            out.append(cells)
    return out


_PLAIN_SPLIT = re.compile(r"\t| {2,}")


def html_rows(html: str) -> Rows:
    parser = _TableText()
    try:
        parser.feed(html)
        return _normalize(parser.finish())
    except _TooComplexError as exc:
        raise MimeError("html too complex") from exc


def plain_rows(text: str) -> Rows:
    lines = text.splitlines()[:MAX_ROWS]
    return _normalize([_PLAIN_SPLIT.split(line.strip()) for line in lines])


def body_rows(bodies: Bodies) -> Rows:
    if bodies.html is not None:
        return html_rows(bodies.html)
    return plain_rows(bodies.plain or "")
