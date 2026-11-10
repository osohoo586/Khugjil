"""arq background worker. Job arguments are ids only; no content ever enters the queue.

Run with ``khugjil worker`` (the class is built at runtime so importing this module never needs
configuration).
"""

from __future__ import annotations

import uuid
from typing import Any

from arq import Retry, func

from khugjil.delivery.notifier import SendResult
from khugjil.delivery.service import fanout, send
from khugjil.logs import configure_logging
from khugjil.runtime import Runtime, arq_redis_settings
from khugjil.settings import Settings

MAX_SEND_TRIES = 6


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    ctx["rt"] = await Runtime.create(ctx["settings"])


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["rt"].close()


async def fanout_job(ctx: dict[str, Any], tenant_id: str, txn_id: str) -> int:
    rt: Runtime = ctx["rt"]
    ids = await fanout(rt.delivery_deps(), uuid.UUID(tenant_id), uuid.UUID(txn_id))
    for delivery_id in ids:
        await ctx["redis"].enqueue_job(
            "send_job", tenant_id, str(delivery_id), _job_id=f"send:{delivery_id}"
        )
    return len(ids)


async def send_job(ctx: dict[str, Any], tenant_id: str, delivery_id: str) -> str:
    rt: Runtime = ctx["rt"]
    result, _ = await send(rt.delivery_deps(), uuid.UUID(tenant_id), uuid.UUID(delivery_id))
    if result is SendResult.RETRY and ctx.get("job_try", 1) < MAX_SEND_TRIES:
        raise Retry(defer=min(60, 2 ** int(ctx.get("job_try", 1))))
    return result.value if result else "noop"


def worker_class(settings: Settings) -> type:
    from khugjil.jobs.cron import cron_jobs

    async def _startup(ctx: dict[str, Any]) -> None:
        ctx["settings"] = settings
        await startup(ctx)

    return type(
        "WorkerSettings",
        (),
        {
            "functions": [
                func(fanout_job, max_tries=3),
                func(send_job, max_tries=MAX_SEND_TRIES),
            ],
            "cron_jobs": cron_jobs(),
            "on_startup": _startup,
            "on_shutdown": shutdown,
            "redis_settings": arq_redis_settings(settings),
            "max_jobs": 20,
            "keep_result": 60,
        },
    )
