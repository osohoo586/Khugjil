from __future__ import annotations

import io
import json
import logging
import uuid

import pytest
from pydantic import ValidationError

from khugjil.crypto import (
    Crypto,
    DecryptError,
    Purpose,
    derive_subkey,
    field_aad,
    length_prefixed,
    new_token,
    token_hash,
)
from khugjil.logs import configure_logging, get_logger
from tests.conftest import make_settings

# ---------------------------------------------------------------- settings


def test_bank_values_have_no_defaults() -> None:
    with pytest.raises(ValidationError):
        make_settings(bank_senders=())
    with pytest.raises(ValidationError):
        make_settings(bank_dkim_domains=())


def test_master_key_must_be_32_bytes() -> None:
    with pytest.raises(ValidationError):
        make_settings(master_key="c2hvcnQ=")


def test_production_refuses_dev_routes_and_agents() -> None:
    with pytest.raises(ValidationError):
        make_settings(env="production", dev_routes=True)
    with pytest.raises(ValidationError):
        make_settings(env="production", agent_devices_enabled=True)


def test_secrets_not_in_repr() -> None:
    s = make_settings()
    assert s.master_key.get_secret_value() not in repr(s)


# ---------------------------------------------------------------- crypto


def test_subkeys_differ_per_purpose() -> None:
    master = b"\x02" * 32
    keys = {derive_subkey(master, p) for p in Purpose}
    assert len(keys) == len(Purpose)


def test_length_prefix_prevents_concatenation_collisions(crypto: Crypto) -> None:
    a = crypto.hmac(Purpose.IDEMPOTENCY, "ab", "c")
    b = crypto.hmac(Purpose.IDEMPOTENCY, "a", "bc")
    assert a != b
    assert length_prefixed("ab", "c") != length_prefixed("a", "bc")


def test_hmac_is_tenant_scoped(crypto: Crypto) -> None:
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    assert crypto.hmac(Purpose.ACCOUNT_HMAC, t1, "2222222222") != crypto.hmac(
        Purpose.ACCOUNT_HMAC, t2, "2222222222"
    )


def test_encrypt_roundtrip_and_aad_binding(crypto: Crypto) -> None:
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    aad1 = field_aad(t1, "bank_account", "number_enc")
    blob = crypto.encrypt("2222222222", aad1)
    assert crypto.decrypt_str(blob, aad1) == "2222222222"
    with pytest.raises(DecryptError):
        crypto.decrypt(blob, field_aad(t2, "bank_account", "number_enc"))
    with pytest.raises(DecryptError):
        crypto.decrypt(blob, field_aad(t1, "transaction", "description_enc"))
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(DecryptError):
        crypto.decrypt(tampered, aad1)


def test_encryption_is_randomized(crypto: Crypto) -> None:
    aad = field_aad(None, "x", "y")
    assert crypto.encrypt("same", aad) != crypto.encrypt("same", aad)


def test_field_key_not_usable_as_hmac(crypto: Crypto) -> None:
    with pytest.raises(ValueError, match="FIELD_ENC"):
        crypto.hmac(Purpose.FIELD_ENC, "x")


def test_tokens() -> None:
    t = new_token()
    assert len(t) >= 43
    assert token_hash(t) == token_hash(t.encode())
    assert "redacted" in repr(Crypto(b"\x03" * 32))


# ---------------------------------------------------------------- logging


def _capture() -> io.StringIO:
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    return buf


def test_log_allowlist_drops_unknown_keys() -> None:
    buf = _capture()
    get_logger().info("ingest.dropped", tenant_id="t1", reason_code="AUTH_FAIL", body="SECRET-BODY")
    line = json.loads(buf.getvalue().strip())
    assert line["reason_code"] == "AUTH_FAIL"
    assert "body" not in line
    assert "SECRET-BODY" not in buf.getvalue()


def test_log_event_name_must_be_constant() -> None:
    buf = _capture()
    get_logger().info("payer name is TESTPAYER")
    assert "TESTPAYER" not in buf.getvalue()
    assert "invalid_event_name" in buf.getvalue()


def test_log_exceptions_without_message() -> None:
    buf = _capture()
    try:
        raise ValueError("description SECRET-DESC")
    except ValueError:
        get_logger().exception("job.failed")
    out = buf.getvalue()
    assert "SECRET-DESC" not in out
    assert "ValueError" in out


def test_stdlib_messages_are_dropped() -> None:
    buf = _capture()
    logging.getLogger("some.lib").warning("RCPT TO:<secret@in.example>")
    out = buf.getvalue()
    assert "secret@in.example" not in out
    assert "stdlib_log" in out
