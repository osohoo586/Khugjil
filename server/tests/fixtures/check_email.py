"""Run a saved bank email (.eml) through the real checks, without spending money on test transfers.

    scripts/check-email.sh FILE.eml            # dry run: which check passes or fails, nothing saved
    scripts/check-email.sh FILE.eml --deliver  # then ingest it for real (stored once, cashier notified)

Uses the running Mac-server settings (.dev/public) and real DNS for DKIM. Prints only the outcome,
the MIME structure, the signing domains and, when accepted, amount, currency, time and which
registered account matched (alias). Never the description, account numbers, balance or any text.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from dkim.dnsplug import get_txt
from sqlalchemy import select

from khugjil.crypto import Purpose, field_aad
from khugjil.db.models import BankAccount
from khugjil.db.tenancy import tenant_session
from khugjil.ingest import mime
from khugjil.ingest.checks import Accepted, AccountMatch, BankConfig, evaluate, masked_match
from khugjil.ingest.dkim_arc import authenticate, signature_domains
from khugjil.ingest.forwarding import shape
from khugjil.ingest.pipeline import ingest
from khugjil.logs import configure_logging
from khugjil.runtime import Runtime
from khugjil.settings import Settings

STATE = Path(__file__).resolve().parents[3] / ".dev" / "public" / "tenant.txt"


def _tenant() -> tuple[uuid.UUID, str]:
    values = dict(line.split("=", 1) for line in STATE.read_text().splitlines() if "=" in line)
    return uuid.UUID(values["tenant_id"]), values["inbound_address"].split("@")[0]


async def main(path: Path, deliver: bool) -> int:
    configure_logging("WARNING")
    settings = Settings()
    raw = path.read_bytes()
    tenant_id, local = _tenant()
    rcpt = f"{local}@{settings.inbound_domain}"
    rt = await Runtime.create(settings)
    try:
        async with tenant_session(rt.sm, tenant_id) as s:
            rows = (await s.execute(select(BankAccount))).scalars().all()
        aad = field_aad(tenant_id, "bank_account", "number_enc")
        numbers = {
            rt.crypto.decrypt_str(r.number_enc, aad): AccountMatch(r.id, r.status == "active")
            for r in rows
        }
        aliases = {r.id: r.alias for r in rows}
        by_hmac = {
            rt.crypto.hmac(Purpose.ACCOUNT_HMAC, tenant_id, n): m for n, m in numbers.items()
        }

        def lookup(value: str) -> AccountMatch | None:
            if "*" in value:
                return masked_match(value, numbers)
            return by_hmac.get(rt.crypto.hmac(Purpose.ACCOUNT_HMAC, tenant_id, value))

        bank = BankConfig.from_settings(settings)
        msg = mime.parse_message(raw)
        print(f"registered accounts: {len(numbers)}")
        print(f"size: {len(raw)} bytes   structure: {shape(msg)}")
        print(f"signed by: {', '.join(signature_domains(raw)) or '-'}   expected: "
              f"{', '.join(sorted(bank.dkim_domains))}")  # fmt: skip
        print(
            f"DKIM/ARC check: {'pass' if authenticate(raw, bank.dkim_domains, get_txt) else 'FAIL'}"
        )
        result = evaluate(raw, bank=bank, lookup_account=lookup, dnsfunc=get_txt)
        if isinstance(result, Accepted):
            print("result: ACCEPTED")
            print(f"  amount: {result.amount} {result.currency}   time: {result.txn_at}")
            print(
                f"  account: {aliases.get(result.account_id, '?')}   parser: {result.parser_version}"
            )
        else:
            print(f"result: {getattr(result, 'reason', 'FORWARDING CODE')}")
        if deliver:
            outcome = await ingest(rt.ingest_deps(get_txt), rcpt, raw, transport="replay")
            print(f"delivered: {outcome.reason}")
    finally:
        await rt.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    sys.exit(asyncio.run(main(Path(sys.argv[1]).expanduser(), "--deliver" in sys.argv[2:])))
