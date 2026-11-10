"""SPEC §3.5: never store or log the raw email; process it in memory only."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.fixtures.emails import FakeTxn, Mailer, build_message

pytestmark = [pytest.mark.invariant(5), pytest.mark.db]

HEADER_MARKER = "RAWHEADERMARKER7731"
FOOTER_MARKER = "RAWFOOTERMARKER9917"


@pytest.mark.parametrize("signed", [True, False], ids=["accepted", "dropped"])
async def test_raw_email_leaves_no_trace(  # type: ignore[no-untyped-def]
    world,
    mailer: Mailer,
    log_capture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signed: bool,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    t = FakeTxn(footer=(FOOTER_MARKER,))
    kw = {
        "extra_headers": (("X-Marker", HEADER_MARKER),),
        "message_id": "<raw-marker@bank.example>",
    }
    raw = mailer.dkim_sign(build_message(t, **kw)) if signed else build_message(t, **kw)  # type: ignore[arg-type]

    outcome = await world.ingest(raw)
    assert str(outcome.reason) == ("ACCEPTED" if signed else "AUTH_FAIL")

    haystacks = [log_capture.getvalue(), await world.dump_all_text(), await world.redis_dump()]
    for hay in haystacks:
        for marker in (HEADER_MARKER, FOOTER_MARKER, "raw-marker@bank.example", "DKIM-Signature"):
            assert marker not in hay
    assert list(tmp_path.iterdir()) == []
