"""Shared fixtures. Everything here is fake: no real bank data, addresses or keys."""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from khugjil.crypto import Crypto
from khugjil.settings import Settings

FAKE_BANK_DOMAIN = "bank.example"
FAKE_BANK_SENDER = "notify@bank.example"
FAKE_SUBJECT = "Test income notification"
FAKE_SUBJECT_RE = r"^Test income notification$"
TEST_MASTER_KEY = base64.b64encode(b"\x01" * 32).decode()

TEST_DATABASE_URL = os.environ.get(
    "KHJ_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:test-only-not-secret@127.0.0.1:55432/khugjil_test",
)
TEST_REDIS_URL = os.environ.get("KHJ_TEST_REDIS_URL", "redis://127.0.0.1:56379/0")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "domain": "khugjil.test",
        "master_key": TEST_MASTER_KEY,
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "bank_senders": (FAKE_BANK_SENDER,),
        "bank_dkim_domains": (FAKE_BANK_DOMAIN,),
        "bank_subject_re": FAKE_SUBJECT_RE,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
def crypto(settings: Settings) -> Crypto:
    return Crypto(settings.master_key_bytes)
