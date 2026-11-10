"""SPEC §3.8: idempotency key; exactly-once delivery per device/channel."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from khugjil.crypto import Crypto
from tests.fixtures.emails import FakeTxn, Mailer
from tests.invariants.helpers import pending

pytestmark = pytest.mark.invariant(8)


@pending("pipeline")
def test_key_is_deterministic_and_input_sensitive(crypto: Crypto) -> None:
    from khugjil.ingest.pipeline import idempotency_key

    t = uuid.uuid4()
    base = idempotency_key(crypto, t, "1234567", "2026.10.15 08:37:22", "2222222222")
    assert base == idempotency_key(crypto, t, "1234567", "2026.10.15 08:37:22", "2222222222")
    variants = [
        idempotency_key(crypto, uuid.uuid4(), "1234567", "2026.10.15 08:37:22", "2222222222"),
        idempotency_key(crypto, t, "1234568", "2026.10.15 08:37:22", "2222222222"),
        idempotency_key(crypto, t, "1234567", "2026.10.15 08:37:23", "2222222222"),
        idempotency_key(crypto, t, "1234567", "2026.10.15 08:37:22", "3333333333"),
    ]
    assert len({base, *variants}) == 5


@pending("pipeline")
@pytest.mark.db
async def test_same_email_twice_is_stored_once(world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    raw = mailer.bank_mail()
    first = await world.ingest(raw)
    second = await world.ingest(raw)
    resent = await world.ingest(mailer.bank_mail(message_id="<resent@bank.example>"))
    assert [str(o.reason) for o in (first, second, resent)] == [
        "ACCEPTED",
        "DUPLICATE",
        "DUPLICATE",
    ]
    assert await world.count("transaction") == 1


@pending("pipeline")
@pytest.mark.db
async def test_concurrent_duplicates_store_once(world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    raw = mailer.bank_mail()
    outcomes = await asyncio.gather(*(world.ingest(raw) for _ in range(5)))
    assert sorted(str(o.reason) for o in outcomes).count("ACCEPTED") == 1
    assert await world.count("transaction") == 1


@pending("delivery")
@pytest.mark.db
async def test_fanout_twice_delivers_once_per_target(world, mailer: Mailer) -> None:  # type: ignore[no-untyped-def]
    outcome = await world.ingest(mailer.bank_mail(FakeTxn(journal="7654321")))
    await asyncio.gather(world.fanout(outcome.transaction_id), world.fanout(outcome.transaction_id))
    rows = await world.rows("delivery")
    keys = [(r["transaction_id"], r["channel"], r["target_id"]) for r in rows]
    assert keys
    assert len(keys) == len(set(keys))
    sent = await world.send_all()
    again = await world.send_all()
    assert len(sent) == len(keys)
    assert again == []
