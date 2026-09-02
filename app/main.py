from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

from .config import Config
from .monitor import Monitor
from .source import MagnitkaSource
from .storage import Storage
from .telegram import TelegramGateway


async def amain() -> None:
    load_dotenv()
    cfg = Config.from_env()
    storage = Storage(cfg.db_path)
    storage.seed_goalies(cfg.seed_path)
    source = MagnitkaSource(
        cfg.base_url,
        cfg.tournament_tz,
        timeout=cfg.request_timeout_seconds,
        candidate_limit=cfg.calendar_candidate_limit,
    )
    telegram = TelegramGateway(cfg.telegram_token, storage)
    monitor = Monitor(cfg, source, storage, telegram)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(monitor.run())
            tg.create_task(telegram.poll_commands(monitor.status_text, monitor.today_text))
    finally:
        await telegram.close()
        await source.close()
        storage.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(amain())


if __name__ == "__main__":
    main()
