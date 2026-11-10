"""Alembic environment. Migrations run as the owner role (KHJ_MIGRATION_DATABASE_URL)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from khugjil.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get(
        "KHJ_MIGRATION_DATABASE_URL", ""
    )
    if not url:
        raise RuntimeError("set KHJ_MIGRATION_DATABASE_URL")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
