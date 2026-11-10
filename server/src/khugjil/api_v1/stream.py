"""Server-Sent Events for a paired device.

The worker publishes ids only; this process re-loads the transaction and re-checks that the
device is still active, that a shift is open and that the account is visible to that shift,
before writing anything. A heartbeat comment goes out every 15 s, and every heartbeat re-checks
the device (backstop for revocation events). At most 2 concurrent streams per device.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Request
from starlette.responses import StreamingResponse

from khugjil.api_v1.cashier import Device_, Rt, fail, router, visible_account_ids
from khugjil.auth.devices import DeviceAuth, open_shift
from khugjil.db.models import Device, Transaction
from khugjil.db.tenancy import tenant_session
from khugjil.delivery.service import notification_for
from khugjil.events import device_channel
from khugjil.runtime import Runtime

HEARTBEAT_S = 15.0
TEST_NOTIFICATION = {
    "test": True,
    "amount_text": "100 ₮",
    "description": "Туршилтын мэдэгдэл",
    "account_alias": None,
}
MAX_STREAMS = 2


def _frame(event: str, data: object, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _device_ok(rt: Runtime, auth: DeviceAuth) -> bool:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        device = await s.get(Device, auth.device_id)
        return device is not None and device.status == "active"


async def _txn_frame(rt: Runtime, auth: DeviceAuth, txn_id: uuid.UUID) -> str | None:
    async with tenant_session(rt.sm, auth.tenant_id) as s:
        shift = await open_shift(s, auth.device_id)
        if shift is None:
            return None
        txn = await s.get(Transaction, txn_id)
        if txn is None or txn.bank_account_id not in await visible_account_ids(
            s, auth.device_id, shift.member_id
        ):
            return None
        note = await notification_for(rt.delivery_deps(), s, auth.tenant_id, txn)
    return _frame("txn", note.to_api(), note.seq)


async def sse_events(
    rt: Runtime,
    auth: DeviceAuth,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_s: float = HEARTBEAT_S,
) -> AsyncIterator[str]:
    pubsub = rt.redis.pubsub()
    await pubsub.subscribe(device_channel(auth.device_id))
    try:
        yield "retry: 3000\n\n"
        while not await is_disconnected():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=heartbeat_s)
            if msg is None:
                if not await _device_ok(rt, auth):
                    yield _frame("device_revoked", {})
                    return
                yield ": hb\n\n"
                continue
            try:
                event = json.loads(msg["data"])
            except (ValueError, TypeError):
                continue
            kind = event.get("type")
            if kind == "txn":
                frame = await _txn_frame(rt, auth, uuid.UUID(event["transaction_id"]))
                if frame:
                    yield frame
            elif kind in ("shift_ended", "settings_changed"):
                yield _frame(kind, {})
            elif kind == "test":
                yield _frame("test", TEST_NOTIFICATION)
            elif kind == "device_revoked":
                yield _frame("device_revoked", {})
                return
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()  # type: ignore[no-untyped-call]


@router.get("/stream")
async def stream(auth: Device_, rt: Rt, request: Request) -> StreamingResponse:
    key = f"khj:streams:{auth.device_id}"
    count = await rt.redis.incr(key)
    await rt.redis.expire(key, 3600)
    if count > MAX_STREAMS:
        await rt.redis.decr(key)
        raise fail(429, "rate_limited")

    async def body() -> AsyncIterator[str]:
        try:
            async for chunk in sse_events(rt, auth, request.is_disconnected):
                yield chunk
        finally:
            await asyncio.shield(rt.redis.decr(key))

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )
