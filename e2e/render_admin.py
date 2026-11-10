"""Render key admin pages from the real Jinja templates with fake data, for screenshot tests.

Usage (from server/): uv run python ../e2e/render_admin.py ../e2e/admin-html
No database is needed; every value below is fake.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from khugjil.admin.core import NAV, env

NOW = datetime(2026, 10, 15, 6, 32, tzinfo=UTC)


def uid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def ctx(role: str = "owner") -> SimpleNamespace:
    member = SimpleNamespace(id=uid(1), display_name="Д. Болд", role=role)
    return SimpleNamespace(
        tenant=SimpleNamespace(name="Жишээ ХХК", accent="green"),
        member=member,
        role=role,
        csrf="0" * 64,
        nav=lambda: [(h, label) for h, label, roles in NAV if role in roles],
        rt=SimpleNamespace(settings=SimpleNamespace(telegram_bot_username="khugjil_bot")),
    )


def member(n: int, name: str, role: str, status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(id=uid(n), display_name=name, role=role, status=status)


def pages() -> dict[str, tuple[str, dict[str, Any]]]:
    sara, tuya, oyun, bold = (
        member(2, "Б. Сараа", "cashier"),
        member(3, "Г. Туяа", "cashier", "invited"),
        member(4, "Н. Оюун", "viewer"),
        member(1, "Д. Болд", "owner"),
    )
    consent = SimpleNamespace(version=2)
    devices = [
        SimpleNamespace(id=uid(10), name="Касс-1", kind="web", last_seen_at=NOW),
        SimpleNamespace(id=uid(11), name="Касс-2", kind="web", last_seen_at=datetime(2026, 10, 15, 4, 5, tzinfo=UTC)),
        SimpleNamespace(id=uid(12), name="Утас-Болд", kind="web", last_seen_at=NOW),
    ]
    groups = [SimpleNamespace(id=uid(20), name="Төв дэлгүүр"), SimpleNamespace(id=uid(21), name="Салбар-2")]
    rows = [
        {"txn_at": datetime(2026, 10, 15, 0, 37, tzinfo=UTC), "account": "Төв дэлгүүр ••2222", "amount": Decimal("2800.00"), "currency": "MNT", "description": "Худалдан авалт"},
        {"txn_at": datetime(2026, 10, 15, 0, 21, tzinfo=UTC), "account": "Салбар-2 ••3333", "amount": Decimal("15000.00"), "currency": "MNT", "description": "Захиалга 4411"},
        {"txn_at": datetime(2026, 10, 15, 0, 5, tzinfo=UTC), "account": "Төв дэлгүүр ••2222", "amount": Decimal("120500.50"), "currency": "MNT", "description": "Түрээсийн төлбөр, 10-р сарын хоол, хүүхдийн цэцэрлэг"},
        {"txn_at": datetime(2026, 10, 14, 23, 41, tzinfo=UTC), "account": "Төв дэлгүүр ••2222", "amount": Decimal("250.00"), "currency": "USD", "description": "Invoice 0091"},
    ]
    pairing = SimpleNamespace(short_code="K7QM-3XRP-9DTA", expires_at=datetime(2026, 10, 15, 6, 42, tzinfo=UTC))
    return {
        "overview": ("overview.html", {
            "last_ok": datetime(2026, 10, 15, 0, 37, tzinfo=UTC), "forwarding_ok": True,
            "devices_online": 3, "devices_total": 4, "offline_names": ["Касс-2"], "failed": 0,
            "today_count": 42, "today_total": Decimal("1284500"),
            "problems": [("Касс-2 төхөөрөмж нэг цагаас дээш холбогдоогүй байна.", "/devices"),
                         ("Г. Туяа урилгаа хүлээн аваагүй байна.", "/members")],
        }),
        "members": ("members.html", {
            "members": [bold, sara, tuya, oyun],
            "member_groups": {bold.id: ["Төв дэлгүүр", "Салбар-2"], sara.id: ["Төв дэлгүүр"], tuya.id: ["Салбар-2"]},
            "consents": {bold.id: [consent], sara.id: [consent]},
            "windows": {bold.id: [SimpleNamespace(weekday=0, start_local=time(9), end_local=time(21))]},
            "linked": {bold.id}, "roles": ["owner", "admin", "viewer", "cashier"],
            "groups": {g.id: g.name for g in groups},
        }),
        "devices": ("devices.html", {
            "devices": devices,
            "device_groups": {uid(10): ["Төв дэлгүүр"], uid(11): ["Салбар-2"], uid(12): ["Төв дэлгүүр", "Салбар-2"]},
            "groups": groups, "on_shift": {uid(10): "Б. Сараа"},
            "online": {uid(10): True, uid(11): False, uid(12): True},
        }),
        "pair": ("pair.html", {
            "pairing": pairing, "url": "https://app.example.mn/pair#t=" + "x" * 43,
            "device_name": "Касс-3", "app_host": "app.example.mn",
        }),
        "email": ("email.html", {
            "notice": None, "address": "k7q2m4x9d3ta6vwe5hb2nrfy@in.example.mn",
            "fwd_code": "123456789", "fwd_expires": datetime(2026, 10, 16, 0, 40, tzinfo=UTC),
            "events": [(datetime(2026, 10, 15, 0, 37, tzinfo=UTC), "Хүлээн авсан"),
                       (datetime(2026, 10, 15, 0, 12, tzinfo=UTC), "Татгалзсан — DKIM баталгаажаагүй"),
                       (datetime(2026, 10, 14, 13, 3, tzinfo=UTC), "Алгассан — давхардсан гүйлгээ")],
            "last_ok": datetime(2026, 10, 15, 0, 37, tzinfo=UTC), "alerts": set(),
            "bank_senders": ("<банкны хаяг>",),
        }),
        "history": ("history.html", {
            "rows": rows, "total": 1284, "page": 1, "pages": 26, "accounts": [],
            "f": {"date_from": "2026-10-01", "date_to": "2026-10-15", "account": "", "amount_min": "", "amount_max": ""},
        }),
    }  # fmt: skip


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    c = ctx()
    for name, (template, values) in pages().items():
        html = env.get_template(template).render(ctx=c, path="/" + name, **values)
        (out / f"{name}.html").write_text(html, encoding="utf-8")
    login = env.get_template("login.html").render(ctx=None, error=None)
    (out / "login.html").write_text(login, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
