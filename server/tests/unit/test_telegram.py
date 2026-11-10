"""Telegram webhook, linking, consent and /stop, against a mocked Bot API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from khugjil.crypto import new_token, token_hash
from khugjil.db.models import TelegramLinkCode
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.channels import TelegramNotifier
from khugjil.delivery.notifier import SendResult, SendTarget
from tests.fixtures.api import ApiHarness
from tests.fixtures.emails import Mailer

pytestmark = pytest.mark.db
SECRET = "test-webhook-secret-not-real"
CHAT = 424242


class BotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={"ok": True, "result": True})

    def texts(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]


@pytest.fixture
async def bot(world: Any, mailer: Mailer) -> AsyncIterator[tuple[ApiHarness, BotApi]]:
    h = await ApiHarness.create(world, mailer.dns)
    api = BotApi()
    await h.rt.http.aclose()
    h.rt.http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    h.rt.settings = h.rt.settings.model_copy(
        update={
            "telegram_bot_token": SecretStr("123:fake-token"),
            "telegram_webhook_secret": SecretStr(SECRET),
        }
    )
    try:
        yield h, api
    finally:
        await h.close()


async def post(h: ApiHarness, update: dict[str, Any], secret: str = SECRET) -> httpx.Response:
    return await h.client.post(
        "/hooks/telegram",
        json=update,
        headers={"x-telegram-bot-api-secret-token": secret, "origin": ""},
    )


def message(text_: str, chat_type: str = "private", chat_id: int = CHAT) -> dict[str, Any]:
    return {"message": {"chat": {"id": chat_id, "type": chat_type}, "text": text_}}


async def link_code(world: Any) -> str:
    code = new_token(12)
    async with tenant_session(world.db.sm, world.tenant_id) as s:
        await s.execute(
            text("UPDATE member SET role = 'owner' WHERE id = :m"), {"m": world.member_id}
        )
        s.add(
            TelegramLinkCode(
                tenant_id=world.tenant_id,
                member_id=world.member_id,
                code_hash=token_hash(code),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    return code


async def test_secret_required(bot: tuple[ApiHarness, BotApi]) -> None:
    h, _ = bot
    assert (await post(h, message("/start x"), secret="wrong")).status_code == 401
    r = await h.client.post("/hooks/telegram", json={}, headers={"origin": ""})
    assert r.status_code == 401


async def test_leaves_groups_and_channels(bot: tuple[ApiHarness, BotApi]) -> None:
    h, api = bot
    for chat_type in ("group", "supergroup", "channel"):
        await post(h, message("hello", chat_type, chat_id=-100))
    joined = {"my_chat_member": {"chat": {"id": -200, "type": "group"}}}
    await post(h, joined)
    assert [m for m, _ in api.calls] == ["leaveChat"] * 4
    assert api.texts() == []


async def test_link_consent_deliver_stop(
    bot: tuple[ApiHarness, BotApi], world: Any, mailer: Mailer
) -> None:
    h, api = bot
    code = await link_code(world)
    await post(h, message(f"/start {code}"))
    sent = [p for m, p in api.calls if m == "sendMessage"][-1]
    assert "parse_mode" not in sent
    assert sent["link_preview_options"] == {"is_disabled": True}
    assert "Мэдэгдэл хүлээн авах зөвшөөрөл" in sent["text"]
    button = sent["reply_markup"]["inline_keyboard"][0][0]
    links = await world.rows("telegram_link")
    assert len(links) == 1
    assert str(CHAT).encode() not in bytes(links[0]["chat_enc"])
    assert not [c for c in await world.rows("consent") if c["channel"] == "telegram"]

    callback = {
        "callback_query": {
            "id": "cb1",
            "data": button["callback_data"],
            "message": {"chat": {"id": CHAT, "type": "private"}},
        }
    }
    await post(h, callback)
    assert [c["channel"] for c in await world.rows("consent")].count("telegram") == 1
    assert "Зөвшөөрөл хадгалагдлаа" in api.texts()[-1]

    notifier = TelegramNotifier(world.db.sm, world.db.crypto, "123:fake-token", h.rt.http)
    target = SendTarget(world.tenant_id, "telegram", world.member_id, world.member_id, None)
    assert await notifier.send_text(target, "Орлого\n2,800 ₮") is SendResult.SENT
    assert api.calls[-1][1]["chat_id"] == CHAT

    await post(h, message("/stop"))
    assert "Мэдэгдэл зогслоо" in api.texts()[-1]
    consents = [c for c in await world.rows("consent") if c["channel"] == "telegram"]
    assert all(c["revoked_at"] is not None for c in consents)
    assert (await world.rows("telegram_link"))[0]["status"] == "revoked"
    assert await notifier.send_text(target, "x") is SendResult.GONE


async def test_code_is_single_use_and_owner_only(
    bot: tuple[ApiHarness, BotApi], world: Any
) -> None:
    h, api = bot
    code = await link_code(world)
    await post(h, message(f"/start {code}"))
    await post(h, message(f"/start {code}", chat_id=999))
    assert "буруу" in api.texts()[-1]
    await post(h, message("/start nonsense-code-123"))
    assert "буруу" in api.texts()[-1]
    await post(h, message("hello"))
    await post(h, message("/start"))
    assert "зөвхөн байгууллагын эзэмшигчид" in api.texts()[-1]


async def test_consent_from_unknown_chat(bot: tuple[ApiHarness, BotApi]) -> None:
    h, api = bot
    callback = {
        "callback_query": {
            "id": "cb",
            "data": "consent:1",
            "message": {"chat": {"id": 55, "type": "private"}},
        }
    }
    await post(h, callback)
    assert "зөвхөн байгууллагын эзэмшигчид" in api.texts()[-1]
    await post(
        h,
        {
            "callback_query": {
                "id": "cb",
                "data": "consent:x",
                "message": {"chat": {"id": 55, "type": "private"}},
            }
        },
    )
    assert (await post(h, {"update_id": 1})).json() == {"ok": True}


async def test_disabled_without_configuration(world: Any, mailer: Mailer) -> None:
    h = await ApiHarness.create(world, mailer.dns)
    try:
        assert (await post(h, message("/start x"))).status_code == 404
    finally:
        await h.close()
