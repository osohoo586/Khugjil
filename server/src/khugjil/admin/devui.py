"""/dev/ui: component inventory (SPEC §8.5). Mounted only when dev routes are enabled outside
production; the settings validator refuses dev_routes in production."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from khugjil.admin.core import env

router = APIRouter()


@router.get("/dev/ui", response_class=HTMLResponse)
async def inventory() -> HTMLResponse:
    sample_rows = [
        {"txn_at": datetime(2026, 10, 15, 0, 37, tzinfo=UTC), "account": "Төв дэлгүүр ••2222",
         "amount": Decimal("2800.00"), "currency": "MNT", "description": "Худалдан авалт"},
        {"txn_at": datetime(2026, 10, 15, 0, 5, tzinfo=UTC), "account": "Салбар-2 ••3333",
         "amount": Decimal("120500.50"), "currency": "MNT",
         "description": "Түрээсийн төлбөр, 10-р сарын хоол, маш урт тайлбар бүхий жишээ мөр"},
    ]  # fmt: skip
    html = env.get_template("devui.html").render(ctx=None, rows=sample_rows)
    return HTMLResponse(html)
