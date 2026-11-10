"""Text normalization.

``clean_text`` is used on every piece of email text before matching: NFC, invisible and control
characters removed, whitespace collapsed. ``sanitize_description`` additionally truncates the
payer-written description to 200 characters (SPEC §3.6). The result is plain text and is never
interpreted as HTML, Markdown or a URL anywhere.
"""

from __future__ import annotations

import unicodedata

MAX_DESCRIPTION = 200
# Cc control, Cf format (zero-width, bidi embeddings/overrides/isolates, BOM), Co private use,
# Cs surrogates, Cn unassigned / noncharacters.
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    out: list[str] = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) not in _REMOVED_CATEGORIES:
            out.append(ch)
    return " ".join("".join(out).split())


def sanitize_description(text: str) -> str:
    cleaned = clean_text(text)
    if len(cleaned) > MAX_DESCRIPTION:
        cleaned = cleaned[:MAX_DESCRIPTION]
        while cleaned and unicodedata.combining(cleaned[-1]):
            cleaned = cleaned[:-1]
        cleaned = cleaned.rstrip()
    return cleaned
