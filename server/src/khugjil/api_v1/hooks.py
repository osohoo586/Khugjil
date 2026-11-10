"""Server-to-server hooks on app.<DOMAIN>/hooks (exempt from the browser Origin check; each has
its own authentication).

/hooks/cf: Cloudflare Email Routing fallback (PLAN Q4). Disabled unless KHJ_CF_INGEST_SECRET is
set. Signature = hex HMAC-SHA256(secret, "<ts>\\n<rcpt>\\n<sha256 hex of body>"), timestamp within
±300 s, each signature accepted once. The raw MIME then goes through the same in-memory
pipeline, including DKIM/ARC verification.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response

from khugjil.api_v1.cashier import Rt, get_holder
from khugjil.crypto import constant_time_eq, sha256_hex
from khugjil.ingest.pipeline import ingest
from khugjil.settings import MAX_MESSAGE_BYTES

router = APIRouter(prefix="/hooks")
MAX_SKEW_S = 300


def cf_signature(secret: str, ts: str, rcpt: str, body: bytes) -> str:
    message = f"{ts}\n{rcpt}\n{sha256_hex(body)}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


async def _read_limited(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413)
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/cf", status_code=202)
async def cloudflare_ingest(
    request: Request,
    rt: Rt,
    x_khj_timestamp: Annotated[str, Header()],
    x_khj_rcpt: Annotated[str, Header(max_length=320)],
    x_khj_signature: Annotated[str, Header(max_length=128)],
) -> Response:
    secret = rt.settings.cf_ingest_secret
    if secret is None:
        raise HTTPException(404)
    try:
        ts = int(x_khj_timestamp)
    except ValueError:
        raise HTTPException(401) from None
    if abs(time.time() - ts) > MAX_SKEW_S:
        raise HTTPException(401)
    body = await _read_limited(request, MAX_MESSAGE_BYTES)
    expected = cf_signature(secret.get_secret_value(), x_khj_timestamp, x_khj_rcpt, body)
    if not constant_time_eq(expected, x_khj_signature):
        raise HTTPException(401)
    if not await rt.redis.set(f"khj:cf:{expected[:32]}", "1", nx=True, ex=2 * MAX_SKEW_S):
        raise HTTPException(409)
    holder = get_holder(request)
    if holder.dnsfunc is None:  # pragma: no cover - production sets dkim's resolver
        raise HTTPException(503)
    await ingest(rt.ingest_deps(holder.dnsfunc), x_khj_rcpt, body, transport="cf")
    return Response(status_code=202)
