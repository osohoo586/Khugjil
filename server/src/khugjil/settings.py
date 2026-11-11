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

_AGENT_VERSION = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")


def parse_agent_version(value: str | None) -> tuple[int, int, int] | None:
    """Strict MAJOR.MINOR.PATCH (no pre-release tags, no leading zeros); None if malformed."""
    match = _AGENT_VERSION.fullmatch(value or "")
    if match is None:
        return None
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


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
    # Which bank's notification layout to parse (one bank per deployment for now).
    bank_parser: Literal["khan_v1", "golomt_v1"] = "khan_v1"

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
    # Local development only: the HTTPS port in front of the app when it cannot be 443 (for example
    # a local Caddy on 8443). Origins and generated links then include it. Refused in production.
    public_port: int | None = Field(default=None, ge=1, le=65535)
    # Phase 2 desktop agent (PLAN-PHASE2.md). Off until the agent ships.
    agent_devices_enabled: bool = False
    agent_min_version: str = "0.1.0"

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

    @field_validator("agent_min_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if parse_agent_version(v) is None:
            raise ValueError("agent_min_version must look like 1.2.3")
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
        if self.env == "production" and self.dev_routes:
            raise ValueError("dev_routes must be disabled in production")
        if self.env == "production" and self.public_port not in (None, 443):
            raise ValueError("public_port is for local development only")
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

    def _origin(self, host: str) -> str:
        port = f":{self.public_port}" if self.public_port not in (None, 443) else ""
        return f"https://{host}{port}"

    @property
    def app_origin(self) -> str:
        """``https://app.<DOMAIN>`` (plus the port in local development)."""
        return self._origin(self.app_host)

    @property
    def admin_origin(self) -> str:
        return self._origin(self.admin_host)

    @property
    def inbound_domain(self) -> str:
        return f"in.{self.domain}"
