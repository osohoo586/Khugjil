"""Telegram message payloads: plain text, no parse_mode, link previews off (SPEC §6.2)."""

from __future__ import annotations

from typing import Any

TELEGRAM_API = "https://api.telegram.org"


def send_message_payload(*, chat_id: int, text: str) -> dict[str, Any]:
    # No "parse_mode" key: Telegram then treats the text as plain text, so payer-written
    # descriptions cannot inject formatting or hidden links.
    return {
        "chat_id": chat_id,
        "text": text[:4096],
        "link_preview_options": {"is_disabled": True},
        "disable_notification": False,
    }
