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
    storage.seed_bets(cfg.bet_seed_path)
    if cfg.owner_telegram_id:
        storage.ensure_owner(cfg.owner_telegram_id)
    source = MagnitkaSource(
        cfg.base_url,
        cfg.tournament_tz,
        timeout=cfg.request_timeout_seconds,
        candidate_limit=cfg.calendar_candidate_limit,
    )
    telegram = TelegramGateway(
        cfg.telegram_token,
        storage,
        owner_telegram_id=cfg.owner_telegram_id,
        invite_valid_hours=cfg.invite_valid_hours,
        display_tz=cfg.tournament_tz,
    )
    await telegram.initialize()
    monitor = Monitor(cfg, source, storage, telegram)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(monitor.run())
            tg.create_task(telegram.poll_commands(monitor.status_text, monitor.today_text, monitor.debug_text))
    finally:
        await telegram.close()
        await source.close()
        storage.close()


def main() -> None:
    # httpx logs include full Telegram API URLs (and therefore the bot token); never print them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(amain())


if __name__ == "__main__":
    main()
