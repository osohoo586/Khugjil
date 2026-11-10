"""Operator command line: ``khugjil <command>``.

Secrets printed by ``keys generate`` go to your terminal only; copy them into the env file you
create yourself. Nothing here sends them anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import secrets
import signal
import sys
from collections.abc import Sequence

from khugjil.logs import configure_logging, get_logger


def _keys_generate() -> int:
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    vapid = Vapid01()
    vapid.generate_keys()
    private = (
        base64.urlsafe_b64encode(
            vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
        )
        .decode()
        .rstrip("=")
    )
    public = (
        base64.urlsafe_b64encode(
            vapid.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        .decode()
        .rstrip("=")
    )
    print("# Copy into your env file. Do not share these values.")
    print(f"KHJ_MASTER_KEY={base64.b64encode(os.urandom(32)).decode()}")
    print(f"KHJ_TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
    print(f"KHJ_CF_INGEST_SECRET={secrets.token_urlsafe(32)}")
    print(f"KHJ_VAPID_PRIVATE_KEY={private}")
    print(f"KHJ_VAPID_PUBLIC_KEY={public}")
    return 0


def _migrate() -> int:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "db" / "migrations"))
    url = os.environ.get("KHJ_MIGRATION_DATABASE_URL")
    if not url:
        print("KHJ_MIGRATION_DATABASE_URL is not set", file=sys.stderr)
        return 2
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    return 0


async def _smtp_main() -> None:
    from dkim.dnsplug import get_txt

    from khugjil.ingest.smtp import IngestHandler, ReloadingTLS, start_server
    from khugjil.ratelimit import RateLimiter
    from khugjil.runtime import Runtime
    from khugjil.settings import Settings

    settings = Settings()
    if settings.env == "production" and not (settings.smtp_tls_cert and settings.smtp_tls_key):
        raise SystemExit("production SMTP requires KHJ_SMTP_TLS_CERT and KHJ_SMTP_TLS_KEY")
    rt = await Runtime.create(settings)
    tls = (
        ReloadingTLS(settings.smtp_tls_cert, settings.smtp_tls_key).get()
        if settings.smtp_tls_cert and settings.smtp_tls_key
        else None
    )
    handler = IngestHandler(
        rt.ingest_deps(get_txt), RateLimiter(rt.redis), settings.smtp_max_sessions_per_ip_per_min
    )
    server = await start_server(
        handler,
        host=settings.smtp_bind,
        port=settings.smtp_port,
        hostname=settings.inbound_domain,
        tls=tls,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    get_logger().info("smtp.started")
    await stop.wait()
    server.close()
    await server.wait_closed()
    await rt.close()


def _worker() -> int:
    from arq import run_worker

    from khugjil.jobs.worker import worker_class
    from khugjil.settings import Settings

    run_worker(worker_class(Settings()))
    return 0


async def _telegram_webhook() -> int:
    from khugjil.api_v1.telegram_hook import set_webhook
    from khugjil.runtime import Runtime
    from khugjil.settings import Settings

    rt = await Runtime.create(Settings())
    try:
        return await set_webhook(rt)
    finally:
        await rt.close()


async def _tenant_create(name: str) -> int:
    from khugjil.runtime import Runtime
    from khugjil.services.provisioning import create_tenant
    from khugjil.settings import Settings

    settings = Settings()
    rt = await Runtime.create(settings)
    try:
        tenant_id, local = await create_tenant(rt.sm, rt.crypto, name)
        print(f"tenant_id={tenant_id}")
        print(f"inbound_address={local}@{settings.inbound_domain}")
        from khugjil.services.invites import create_owner_invite

        print(f"owner_invite={await create_owner_invite(rt, tenant_id)}")
    finally:
        await rt.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="khugjil")
    sub = parser.add_subparsers(dest="cmd", required=True)
    keys = sub.add_parser("keys", help="key material")
    keys.add_argument("action", choices=["generate"])
    sub.add_parser("migrate", help="apply database migrations (owner role)")
    sub.add_parser("smtp", help="run the inbound SMTP server")
    sub.add_parser("worker", help="run the background worker")
    tg = sub.add_parser("telegram", help="Telegram bot")
    tg.add_argument("action", choices=["set-webhook"])
    tenant = sub.add_parser("tenant", help="tenant administration")
    tenant.add_argument("action", choices=["create"])
    tenant.add_argument("--name", required=True)
    args = parser.parse_args(argv)

    configure_logging()
    if args.cmd == "keys":
        return _keys_generate()
    if args.cmd == "migrate":
        return _migrate()
    if args.cmd == "smtp":
        asyncio.run(_smtp_main())
        return 0
    if args.cmd == "worker":
        return _worker()
    if args.cmd == "telegram":
        return asyncio.run(_telegram_webhook())
    return asyncio.run(_tenant_create(args.name))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
