"""SPEC §3.4: never extract, store, show or send balance, payer name or payer account."""

from __future__ import annotations

import dataclasses

import pytest

from tests.fixtures.emails import PAYER_ACCOUNT, TENANT_ACCOUNT, Mailer
from tests.invariants.helpers import evaluate

pytestmark = pytest.mark.invariant(4)

MARKERS = [PAYER_ACCOUNT, "TESTPAYERMARKER", "TESTSELLERMARKER", "9,876,543.21", "9876543.21"]
FORBIDDEN_FIELD_WORDS = ("payer", "from_account", "balance", "seller", "name", "rate")


def test_accepted_result_has_no_payer_fields(mailer: Mailer) -> None:
    from khugjil.ingest.checks import Accepted

    r = evaluate(mailer.bank_mail(), mailer)
    assert isinstance(r, Accepted)
    for f in dataclasses.fields(Accepted):
        assert not any(w in f.name for w in FORBIDDEN_FIELD_WORDS), f.name
    text = repr(r)
    for marker in [*MARKERS, TENANT_ACCOUNT]:
        assert marker not in text


@pytest.mark.db
async def test_nothing_downstream_contains_payer_data(world, mailer: Mailer, log_capture) -> None:  # type: ignore[no-untyped-def]
    outcome = await world.ingest(mailer.bank_mail())
    assert str(outcome.reason) == "ACCEPTED"
    await world.fanout(outcome.transaction_id)
    sent = await world.send_all()
    assert sent, "expected at least one delivery"
    haystacks = [
        await world.dump_all_text(),
        log_capture.getvalue(),
        *[repr(s) for s in sent],
        await world.redis_dump(),
        await world.api_transactions_json(),
    ]
    for hay in haystacks:
        for marker in [*MARKERS, TENANT_ACCOUNT]:
            assert marker not in hay
