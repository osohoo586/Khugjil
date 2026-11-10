"""Runtime configuration.

All values come from environment variables prefixed ``KHJ_`` (docker compose loads them from
the operator-created env file). Secrets are ``SecretStr`` so they never appear in reprs or logs.
The Khan Bank values (sender, DKIM domain, subject) deliberately have no defaults: the app
refuses to start until the operator provides them (SPEC §12.5, PLAN Q1-Q3).
"""

from __future__ import annotations

import base64
import binascii
import re
from functools import cached_property
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_MESSAGE_BYTES = 256 * 1024
BANK_TZ = "Asia/Ulaanbaatar"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KHJ_", extra="ignore", frozen=True)

    env: Literal["development", "test", "production"] = "production"
    domain: str = Field(min_length=3)
    master_key: SecretStr
    database_url: SecretStr
    redis_url: SecretStr

    # Khan Bank placeholders (PLAN Q1-Q3). No defaults on purpose.
    bank_senders: tuple[str, ...] = Field(min_length=1)
    bank_dkim_domains: tuple[str, ...] = Field(min_length=1)
    bank_subject_re: str = Field(min_length=1)

    retention_days: int = Field(default=90, ge=1, le=3650)
    ingest_event_retention_days: int = Field(default=30, ge=1, le=365)

    # Inside the container only; the host maps 25 -> 2525.
    smtp_bind: str = "0.0.0.0"  # noqa: S104  # nosec B104
    smtp_port: int = 2525
    smtp_tls_cert: str | None = None
    smtp_tls_key: str | None = None
    smtp_max_sessions_per_ip_per_min: int = 30

    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_bot_username: str | None = None

    vapid_private_key: SecretStr | None = None
    vapid_public_key: str | None = None
    vapid_subject: str | None = None

    cf_ingest_secret: SecretStr | None = None

    dev_routes: bool = False
    agent_devices_enabled: bool = False  # Phase 2; must stay False until approved.

    @field_validator("bank_senders", "bank_dkim_domains")
    @classmethod
    def _lower(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(x.strip().lower() for x in v if x.strip())
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("bank_subject_re")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v

    @field_validator("master_key")
    @classmethod
    def _key_len(cls, v: SecretStr) -> SecretStr:
        try:
            raw = base64.b64decode(v.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("master_key must be base64") from exc
        if len(raw) != 32:
            raise ValueError("master_key must decode to exactly 32 bytes")
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production":
            if self.dev_routes:
                raise ValueError("dev_routes must be disabled in production")
            if self.agent_devices_enabled:
                raise ValueError("agent devices are Phase 2 and not enabled")
        return self

    @cached_property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_key.get_secret_value())

    @cached_property
    def subject_pattern(self) -> re.Pattern[str]:
        return re.compile(self.bank_subject_re)

    @property
    def app_host(self) -> str:
        return f"app.{self.domain}"

    @property
    def admin_host(self) -> str:
        return f"admin.{self.domain}"

    @property
    def inbound_domain(self) -> str:
        return f"in.{self.domain}"
