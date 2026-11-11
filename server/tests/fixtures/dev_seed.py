"""Demo data for the local development stack (scripts/dev-seed.sh). Development only.

Everything is fake: the account numbers are the test fixtures' (2222222222, 3333333333), and the
names and descriptions are made up. Bank emails are signed with throwaway test keys and go
through the real ingest pipeline (all checks, the parser, idempotency, fan-out), so the result
looks exactly like real traffic.

    python -m tests.fixtures.dev_seed                       # accounts, groups, cashiers, history
    python -m tests.fixtures.dev_seed txn [AMOUNT] [TEXT]   # one new transaction now
    python -m tests.fixtures.dev_seed tenant NAME           # a new shop (browser flow tests)

The shop is read from (and `tenant` writes to) KHJ_DEV_TENANT_FILE, default .dev/tenant.txt.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from khugjil.auth.secrets import hash_secret
from khugjil.db.models import (
    BankAccount,
    Consent,
    GroupAccount,
    GroupMember,
    Member,
    RecipientGroup,
)
from khugjil.db.tenancy import tenant_session
from khugjil.ingest.pipeline import ingest
from khugjil.runtime import Runtime
from khugjil.services.invites import create_owner_invite
from khugjil.services.provisioning import add_bank_account, create_tenant
from khugjil.settings import BANK_TZ, Settings
from tests.fixtures.emails import TENANT_ACCOUNT, TENANT_ACCOUNT_2, FakeTxn, Mailer

DEFAULT_STATE = Path(__file__).resolve().parents[3] / ".dev" / "tenant.txt"
DEMO_PIN = "482913"
MAIN, BRANCH = "Төв дэлгүүр", "Салбар-2"
ACCOUNTS = {MAIN: TENANT_ACCOUNT, BRANCH: TENANT_ACCOUNT_2}
CASHIERS = {"Б. Сараа": (MAIN, BRANCH), "Г. Туяа": (BRANCH,)}

# (day offset, HH:MM, amount, currency, account, description)
HISTORY: list[tuple[int, str, str, str, str, str]] = [
    (-1, "09:14", "45000", "MNT", MAIN, "Хувцас"),
    (-1, "10:02", "3000", "MNT", BRANCH, "Талх, сүү"),
    (-1, "11:47", "120500.50", "MNT", MAIN, "Түрээсийн төлбөр, 11-р сар"),
    (-1, "14:20", "250", "USD", MAIN, "Invoice 0091"),
    (-1, "16:05", "18000", "MNT", BRANCH, "Ном"),
    (-1, "19:31", "7500", "MNT", MAIN, "Кофе 3ш"),
    (0, "08:21", "15000", "MNT", BRANCH, "Захиалга 4411"),
    (0, "08:37", "2800", "MNT", MAIN, "Худалдан авалт"),
    (0, "09:05", "64000", "MNT", MAIN, "Засварын үйлчилгээ"),
    (
        0,
        "10:12",
        "3000",
        "MNT",
        BRANCH,
        "Б. Батаагийн хүүхдийн цэцэрлэгийн төлбөр, 11-р сарын хоол",
    ),
    (0, "11:40", "9900", "MNT", MAIN, "Хүргэлт"),
    (0, "12:58", "150000", "MNT", MAIN, "Урьдчилгаа"),
    (0, "14:16", "4200", "MNT", BRANCH, "Талх"),
    (0, "15:33", "27500", "MNT", MAIN, "Тоглоом"),
    (0, "17:09", "11000", "MNT", BRANCH, "Цэцэг"),
]


@dataclass(frozen=True)
class Tenant:
    tenant_id: uuid.UUID
    inbound_address: str


def tenant_file() -> Path:
    return Path(os.environ.get("KHJ_DEV_TENANT_FILE") or DEFAULT_STATE)


def load_tenant() -> Tenant:
    text = tenant_file().read_text()
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return Tenant(uuid.UUID(values["tenant_id"]), values["inbound_address"])


def money(amount: str) -> str:
    """2800 -> '2,800.00', the bank's format."""
    return f"{Decimal(amount):,.2f}"


def bank_txn(
    when: datetime, journal: str, amount: str, currency: str, account: str, text: str
) -> FakeTxn:
    stamp = when.strftime("%Y.%m.%d %H:%M:%S")
    return FakeTxn(
        date=stamp,
        system_date=stamp,
        journal=journal,
        from_amount=money(amount),
        from_currency=currency,
        to_amount=money(amount),
        to_currency=currency,
        to_account=ACCOUNTS[account],
        description=text,
    )


async def setup_shop(rt: Runtime, tenant: Tenant) -> None:
    """Accounts, groups and cashiers; safe to run again."""
    tid = tenant.tenant_id
    async with tenant_session(rt.sm, tid) as s:
        accounts = {a.alias: a.id for a in (await s.execute(select(BankAccount))).scalars()}
        for alias, number in ACCOUNTS.items():
            if alias not in accounts:
                accounts[alias] = await add_bank_account(s, rt.crypto, tid, number, alias)
        groups = {g.name: g.id for g in (await s.execute(select(RecipientGroup))).scalars()}
        for alias in ACCOUNTS:
            if alias not in groups:
                group = RecipientGroup(tenant_id=tid, name=alias)
                s.add(group)
                await s.flush()
                groups[alias] = group.id
                s.add(GroupAccount(tenant_id=tid, group_id=group.id, account_id=accounts[alias]))
        members = {m.display_name for m in (await s.execute(select(Member))).scalars()}
        for name, member_groups in CASHIERS.items():
            if name in members:
                continue
            member = Member(
                tenant_id=tid,
                display_name=name,
                role="cashier",
                status="active",
                pin_hash=hash_secret(DEMO_PIN),
            )
            s.add(member)
            await s.flush()
            s.add(Consent(tenant_id=tid, member_id=member.id, version=1, channel="web"))
            for g in member_groups:
                s.add(GroupMember(tenant_id=tid, group_id=groups[g], member_id=member.id))


async def send(rt: Runtime, tenant: Tenant, mailer: Mailer, txn: FakeTxn) -> str:
    outcome = await ingest(
        rt.ingest_deps(mailer.dns), tenant.inbound_address, mailer.bank_mail(txn), "dev"
    )
    return str(outcome.reason)


async def seed() -> None:
    rt, tenant, mailer = await _start()
    try:
        await setup_shop(rt, tenant)
        now = datetime.now(ZoneInfo(BANK_TZ))
        results: dict[str, int] = {}
        for i, (day, hhmm, amount, currency, account, text) in enumerate(HISTORY):
            hour, minute = (int(x) for x in hhmm.split(":"))
            when = (now + timedelta(days=day)).replace(
                hour=hour, minute=minute, second=i, microsecond=0
            )
            if when > now:
                continue  # never in the future
            journal = f"7{when:%m%d}{i:03d}"
            reason = await send(
                rt, tenant, mailer, bank_txn(when, journal, amount, currency, account, text)
            )
            results[reason] = results.get(reason, 0) + 1
        print("transactions:", ", ".join(f"{k} {v}" for k, v in sorted(results.items())))
        print(f"cashiers: {', '.join(CASHIERS)} (demo PIN {DEMO_PIN})")
        print(f"accounts: {MAIN} ••{TENANT_ACCOUNT[-4:]}, {BRANCH} ••{TENANT_ACCOUNT_2[-4:]}")
    finally:
        await rt.close()


async def one(amount: str, text: str) -> None:
    rt, tenant, mailer = await _start()
    try:
        await setup_shop(rt, tenant)
        now = datetime.now(ZoneInfo(BANK_TZ)).replace(microsecond=0)
        journal = f"8{int(time.time() * 1000) % 10**12:012d}"
        reason = await send(rt, tenant, mailer, bank_txn(now, journal, amount, "MNT", MAIN, text))
        print(f"{money(amount)} MNT «{text}» -> {reason}")
    finally:
        await rt.close()


async def new_tenant(name: str) -> None:
    path = tenant_file()
    if path == DEFAULT_STATE:
        raise SystemExit("set KHJ_DEV_TENANT_FILE; the demo shop's file is kept as it is")
    rt = await _runtime()
    try:
        tenant_id, local = await create_tenant(rt.sm, rt.crypto, name)
        invite = await create_owner_invite(rt, tenant_id)
        path.write_text(
            f"tenant_id={tenant_id}\ninbound_address={local}@{rt.settings.inbound_domain}\n"
            f"owner_invite={invite}\n"
        )
    finally:
        await rt.close()


async def _start() -> tuple[Runtime, Tenant, Mailer]:
    return await _runtime(), load_tenant(), Mailer()


async def _runtime() -> Runtime:
    from khugjil.logs import configure_logging

    configure_logging("WARNING")  # the per-email ingest lines are noise here
    settings = Settings()
    if settings.env != "development" or not settings.domain.endswith(".localhost"):
        raise SystemExit("dev_seed runs only against the local development stack")
    return await Runtime.create(settings)


def main(argv: list[str]) -> None:
    if argv[:1] == ["tenant"] and len(argv) == 2:
        asyncio.run(new_tenant(argv[1]))
    elif argv[:1] == ["txn"]:
        amount = argv[1] if len(argv) > 1 else "12500"
        text = argv[2] if len(argv) > 2 else "Худалдан авалт"
        asyncio.run(one(amount, text))
    elif not argv:
        asyncio.run(seed())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
