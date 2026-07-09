"""Command line entry point for the queue worker."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import NoReturn

from redis.asyncio import Redis

from core.queue import redis_settings_from_env
from worker.app import Worker
from worker.config import config_from_env
from worker.processor import EchoProcessor


def main() -> NoReturn:
    """Run the worker process."""

    handler = logging.StreamHandler()
    handler.addFilter(_TraceIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s trace_id=%(trace_id)s %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        force=True,
    )
    asyncio.run(_run())
    raise SystemExit(0)


async def _run() -> None:
    settings = redis_settings_from_env()
    config = config_from_env()
    queue_redis = Redis.from_url(settings.queue_url, decode_responses=True)
    cache_redis = Redis.from_url(settings.cache_url, decode_responses=True)
    worker = Worker(
        queue_redis=queue_redis,
        cache_redis=cache_redis,
        processor=EchoProcessor(),
        send_reply=_log_reply,
        send_typing=_log_typing,
        notify_admin=_log_admin,
        config=config,
    )
    _install_signal_handlers(worker)
    try:
        await worker.run()
    except KeyboardInterrupt:
        worker.request_stop()
    finally:
        await queue_redis.aclose()
        await cache_redis.aclose()


def _install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, worker.request_stop)
        except NotImplementedError:
            signal.signal(signal_number, lambda _signum, _frame: worker.request_stop())


async def _log_reply(chat_id: int, text: str) -> None:
    logging.getLogger(__name__).info("reply chat_id=%s text=%s", chat_id, text)


async def _log_typing(chat_id: int) -> None:
    logging.getLogger(__name__).info("typing chat_id=%s", chat_id)


async def _log_admin(text: str) -> None:
    logging.getLogger(__name__).warning("admin notification: %s", text)


class _TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = "-"
        return True


if __name__ == "__main__":
    main()
