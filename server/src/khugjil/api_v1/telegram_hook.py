"""Telegram webhook (SPEC §6.2): app.<DOMAIN>/hooks/telegram.

* Authenticated by the ``X-Telegram-Bot-Api-Secret-Token`` header (constant-time compare).
* Private chats only (PLAN Q26): the bot leaves every group, supergroup or channel at once.
* ``/start <code>`` links the chat to the owner who generated the one-time code, then asks for
  consent with a button; deliveries start only after the button is pressed.
* ``/stop`` revokes the Telegram consent immediately.
* Every message is plain text: no parse_mode, link previews disabled. Chat ids are stored
  encrypted with a separate HMAC for lookup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select

from khugjil.api_v1.cashier import Rt
from khugjil.crypto import Purpose, constant_time_eq, field_aad, token_hash
from khugjil.db.models import Member, TelegramLink, TelegramLinkCode, Tenant
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.telegram import TELEGRAM_API, send_message_payload
from khugjil.logs import get_logger
from khugjil.runtime import Runtime
from khugjil.services.consent import current_document, give_consent, render_document, revoke_consent

router = APIRouter(prefix="/hooks")
CONSENT_PREFIX = "consent:"

HELP = (
    "Энэ бот зөвхөн байгууллагын эзэмшигчид орлогын мэдэгдэл илгээнэ. "
    "Холбохын тулд админ хэсгийн «Хэрэглэгчид» хуудаснаас «Telegram-аа холбох» товчийг дарна уу."
)
INVALID_CODE = "Холбох код буруу эсвэл хугацаа нь дууссан байна. Админ хэсгээс шинэ код авна уу."
CONSENTED = (
    "Зөвшөөрөл хадгалагдлаа. Ажлын хуваарийн дагуу орлогын мэдэгдэл энд ирнэ. "
    "Зогсоох бол /stop гэж бичнэ үү."
)
STOPPED = "Мэдэгдэл зогслоо. Дахин авах бол админ хэсгээс шинэ холбох код авна уу."


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._base = f"{TELEGRAM_API}/bot{token}"

    async def call(self, method: str, payload: dict[str, Any]) -> None:
        try:
            await self._http.post(f"{self._base}/{method}", json=payload, timeout=10)
        except httpx.HTTPError:
            get_logger().warning("telegram.call_failed", error_code=method)

    async def send(
        self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None
    ) -> None:
        payload = send_message_payload(chat_id=chat_id, text=text)
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in buttons]
                ]
            }
        await self.call("sendMessage", payload)


def chat_hmac(rt: Runtime, chat_id: int) -> bytes:
    return rt.crypto.hmac(Purpose.CHAT_HMAC, str(chat_id))


async def _link(rt: Runtime, client: TelegramClient, chat_id: int, code: str) -> None:
    now = datetime.now(UTC)
    found = await rt.resolver.telegram_code(token_hash(code))
    if found is None:
        await client.send(chat_id, INVALID_CODE)
        return
    tenant_id, code_id = found
    async with tenant_session(rt.sm, tenant_id) as s:
        row = await s.get(TelegramLinkCode, code_id, with_for_update=True)
        member = await s.get(Member, row.member_id) if row else None
        if (
            row is None
            or row.used_at
            or row.expires_at <= now
            or member is None
            or member.role != "owner"
            or member.status != "active"
        ):
            valid = False
        else:
            valid = True
            row.used_at = now
            existing = (
                await s.execute(select(TelegramLink).where(TelegramLink.member_id == member.id))
            ).scalar_one_or_none()
            if existing is not None:
                await s.delete(existing)
                await s.flush()
            s.add(
                TelegramLink(
                    tenant_id=tenant_id,
                    member_id=member.id,
                    chat_hmac=chat_hmac(rt, chat_id),
                    chat_enc=rt.crypto.encrypt(
                        str(chat_id), field_aad(tenant_id, "telegram_link", "chat_enc")
                    ),
                    linked_at=now,
                )
            )
            tenant = await s.get(Tenant, tenant_id)
            doc = await current_document(s)
            text = render_document(doc, tenant.name if tenant else "", rt.settings.retention_days)
            version = doc.version
    if not valid:
        await client.send(chat_id, INVALID_CODE)
        return
    await client.send(chat_id, text, buttons=[("Зөвшөөрч байна", f"{CONSENT_PREFIX}{version}")])


async def _resolve_chat(rt: Runtime, chat_id: int) -> tuple[uuid.UUID, uuid.UUID] | None:
    return await rt.resolver.telegram_chat(chat_hmac(rt, chat_id))


async def _consent(
    rt: Runtime, client: TelegramClient, chat_id: int, data: str, callback_id: str
) -> None:
    await client.call("answerCallbackQuery", {"callback_query_id": callback_id})
    found = await _resolve_chat(rt, chat_id)
    try:
        version = int(data.removeprefix(CONSENT_PREFIX))
    except ValueError:
        return
    if found is None:
        await client.send(chat_id, HELP)
        return
    tenant_id, member_id = found
    try:
        async with tenant_session(rt.sm, tenant_id) as s:
            await give_consent(s, tenant_id, member_id, "telegram", version)
    except ValueError:
        await client.send(chat_id, INVALID_CODE)
        return
    await client.send(chat_id, CONSENTED)


async def _stop(rt: Runtime, client: TelegramClient, chat_id: int) -> None:
    found = await _resolve_chat(rt, chat_id)
    if found is not None:
        tenant_id, member_id = found
        await revoke_consent(
            rt.sm,
            rt.bus,
            tenant_id,
            member_id,
            via="telegram",
            now=datetime.now(UTC),
            channel="telegram",
        )
    await client.send(chat_id, STOPPED)


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    rt: Rt,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    secret = rt.settings.telegram_webhook_secret
    token = rt.settings.telegram_bot_token
    if secret is None or token is None:
        raise HTTPException(404)
    if not x_telegram_bot_api_secret_token or not constant_time_eq(
        secret.get_secret_value(), x_telegram_bot_api_secret_token
    ):
        raise HTTPException(401)
    update: dict[str, Any] = await request.json()
    client = TelegramClient(rt.http, token.get_secret_value())

    chat_member = update.get("my_chat_member")
    message = update.get("message")
    callback = update.get("callback_query")
    chat: dict[str, Any] = {}
    if chat_member:
        chat = chat_member.get("chat", {})
    elif message:
        chat = message.get("chat", {})
    elif callback:
        chat = (callback.get("message") or {}).get("chat", {})
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return {"ok": True}
    if chat.get("type") != "private":
        await client.call("leaveChat", {"chat_id": chat_id})
        return {"ok": True}

    if callback:
        data = str(callback.get("data", ""))
        if data.startswith(CONSENT_PREFIX):
            await _consent(rt, client, chat_id, data, str(callback.get("id", "")))
        return {"ok": True}
    if message:
        text = str(message.get("text", "")).strip()
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and 8 <= len(parts[1]) <= 64:
                await _link(rt, client, chat_id, parts[1])
            else:
                await client.send(chat_id, HELP)
        elif text == "/stop":
            await _stop(rt, client, chat_id)
        else:
            await client.send(chat_id, HELP)
    return {"ok": True}


async def set_webhook(rt: Runtime) -> int:
    """Register the webhook (run once from the CLI after deploying)."""
    token, secret = rt.settings.telegram_bot_token, rt.settings.telegram_webhook_secret
    if token is None or secret is None:
        return 2
    resp = await rt.http.post(
        f"{TELEGRAM_API}/bot{token.get_secret_value()}/setWebhook",
        json={
            "url": f"{rt.settings.app_origin}/hooks/telegram",
            "secret_token": secret.get_secret_value(),
            "allowed_updates": ["message", "callback_query", "my_chat_member"],
            "drop_pending_updates": True,
        },
        timeout=15,
    )
    return 0 if resp.status_code == 200 else 1
