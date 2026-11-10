"""Helpers shared by the SPEC §3 invariant tests.

Imports of application modules happen inside functions, so a test for a feature that does not
exist yet fails (and is reported as xfail) instead of breaking collection.
"""

from __future__ import annotations

import re
import tomllib
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.emails import TENANT_ACCOUNT, TENANT_ACCOUNT_2, Mailer

SERVER_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = SERVER_DIR / "src" / "khugjil"

ACCOUNT_ID = uuid.UUID(int=1)
ACCOUNT_2_ID = uuid.UUID(int=2)


def pending(what: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=f"pending implementation: {what}")


def locked_packages() -> set[str]:
    data = tomllib.loads((SERVER_DIR / "uv.lock").read_text())
    return {re.sub(r"[-_.]+", "-", p["name"].lower()) for p in data.get("package", [])}


def evaluate(
    raw: bytes,
    mailer: Mailer,
    *,
    accounts: dict[str, Any] | None = None,
    include_own_transfers: bool = False,
) -> Any:
    from khugjil.ingest.checks import AccountMatch, BankConfig, evaluate
    from tests.conftest import make_settings

    if accounts is None:
        accounts = {TENANT_ACCOUNT: AccountMatch(account_id=ACCOUNT_ID, active=True)}
    bank = BankConfig.from_settings(make_settings())
    return evaluate(
        raw,
        bank=bank,
        lookup_account=accounts.get,
        dnsfunc=mailer.dns,
        include_own_transfers=include_own_transfers,
    )


def two_accounts() -> dict[str, Any]:
    from khugjil.ingest.checks import AccountMatch

    return {
        TENANT_ACCOUNT: AccountMatch(account_id=ACCOUNT_ID, active=True),
        TENANT_ACCOUNT_2: AccountMatch(account_id=ACCOUNT_2_ID, active=True),
    }
