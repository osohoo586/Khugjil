"""Channel implementations: SSE (via the event bus), Web Push (VAPID) and Telegram."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from sqlalchemy import select

from khugjil.crypto import Crypto, field_aad
from khugjil.db.models import PushSubscription, TelegramLink
from khugjil.db.tenancy import SessionMaker, tenant_session
from khugjil.delivery.format import Notification
from khugjil.delivery.notifier import Channel, SendResult, SendTarget
from khugjil.delivery.telegram import TELEGRAM_API, send_message_payload
from khugjil.events import EventBus


class SseNotifier:
    channel: ClassVar[Channel] = "sse"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def send(self, target: SendTarget, note: Notification) -> SendResult:
        await self._bus.publish(
            target.target_id, "txn", transaction_id=note.transaction_id, seq=str(note.seq)
        )
        return SendResult.SENT


@dataclass(frozen=True)
class VapidConfig:
    private_key: str
    subject: str


class WebPushNotifier:
    channel: ClassVar[Channel] = "webpush"

    def __init__(self, sm: SessionMaker, crypto: Crypto, vapid: VapidConfig | None) -> None:
        self._sm = sm
        self._crypto = crypto
        self._vapid = vapid

    async def send(self, target: SendTarget, note: Notification) -> SendResult:
        if self._vapid is None or target.shift_id is None:
            return SendResult.GONE
        async with tenant_session(self._sm, target.tenant_id) as s:
            subs = list(
                (
                    await s.execute(
                        select(PushSubscription).where(
                            PushSubscription.device_id == target.target_id,
                            PushSubscription.shift_id == target.shift_id,
                        )
                    )
                ).scalars()
            )
        if not subs:
            return SendResult.GONE
        payload = json.dumps(
            {
                "title": "Орлого",
                "body": "\n".join(note.lines()[1 if not note.test else 2 :]),
                "tag": str(note.transaction_id),
                "test": note.test,
            },
            ensure_ascii=False,
        )
        results = [await self._push_one(target, sub, payload) for sub in subs]
        if SendResult.SENT in results:
            return SendResult.SENT
        return SendResult.RETRY if SendResult.RETRY in results else SendResult.GONE

    async def _push_one(
        self, target: SendTarget, sub: PushSubscription, payload: str
    ) -> SendResult:
        from pywebpush import WebPushException, webpush

        if self._vapid is None:
            return SendResult.GONE
        aad = field_aad(target.tenant_id, "push_subscription", "endpoint_enc")
        info: dict[str, Any] = {
            "endpoint": self._crypto.decrypt_str(sub.endpoint_enc, aad),
            "keys": {
                "p256dh": sub.p256dh,
                "auth": self._crypto.decrypt_str(
                    sub.auth_secret_enc,
                    field_aad(target.tenant_id, "push_subscription", "auth_secret_enc"),
                ),
            },
        }
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=info,
                data=payload,
                vapid_private_key=self._vapid.private_key,
                vapid_claims={"sub": self._vapid.subject},
                ttl=120,
                timeout=10,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                async with tenant_session(self._sm, target.tenant_id) as s:
                    await s.delete(await s.merge(sub))
                return SendResult.GONE
            return SendResult.RETRY
        return SendResult.SENT


class TelegramNotifier:
    channel: ClassVar[Channel] = "telegram"

    def __init__(
        self, sm: SessionMaker, crypto: Crypto, token: str | None, client: httpx.AsyncClient
    ) -> None:
        self._sm = sm
        self._crypto = crypto
        self._token = token
        self._client = client

    async def send(self, target: SendTarget, note: Notification) -> SendResult:
        return await self.send_text(target, note.plain_text())

    async def send_text(self, target: SendTarget, text: str) -> SendResult:
        if not self._token:
            return SendResult.GONE
        async with tenant_session(self._sm, target.tenant_id) as s:
            link = (
                await s.execute(
                    select(TelegramLink).where(
                        TelegramLink.member_id == target.member_id, TelegramLink.status == "active"
                    )
                )
            ).scalar_one_or_none()
        if link is None:
            return SendResult.GONE
        chat_id = int(
            self._crypto.decrypt_str(
                link.chat_enc, field_aad(target.tenant_id, "telegram_link", "chat_enc")
            )
        )
        try:
            resp = await self._client.post(
                f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                json=send_message_payload(chat_id=chat_id, text=text),
                timeout=10,
            )
        except httpx.HTTPError:
            return SendResult.RETRY
        if resp.status_code == 200:
            return SendResult.SENT
        if resp.status_code in (400, 403):
            return SendResult.GONE
        return SendResult.RETRY
