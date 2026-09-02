from __future__ import annotations

import asyncio
import logging
from datetime import date
from zoneinfo import ZoneInfo

import httpx

from .models import GameSnapshot
from .parser import extract_game_links, parse_game_page

log = logging.getLogger(__name__)


class MagnitkaSource:
    def __init__(self, base_url: str, tournament_tz: ZoneInfo, timeout: float = 15.0, candidate_limit: int = 30):
        self.base_url = base_url.rstrip("/")
        self.tournament_tz = tournament_tz
        self.candidate_limit = candidate_limit
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "MagnitkaBot1/0.1 (+personal Telegram monitor; low request rate)",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str) -> str:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.client.get(url)
                r.raise_for_status()
                return r.text
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        assert last is not None
        raise last

    async def fetch_game(self, url: str) -> GameSnapshot:
        html = await self._get(url)
        return parse_game_page(html, url, self.tournament_tz)

    async def discover_games_for_date(self, target: date) -> list[GameSnapshot]:
        html = await self._get(f"{self.base_url}/calendar/")
        links = extract_game_links(html, self.base_url)
        # The calendar is descending/current; visiting a small recent window is cheap and robust
        # against changes in table CSS/classes.
        links = links[: self.candidate_limit]
        sem = asyncio.Semaphore(6)

        async def one(url: str) -> GameSnapshot | None:
            async with sem:
                try:
                    return await self.fetch_game(url)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Game discovery fetch failed %s: %s", url, exc)
                    return None

        snaps = await asyncio.gather(*(one(url) for _, url in links))
        result = [s for s in snaps if s and s.scheduled_at and s.scheduled_at.date() == target]
        result.sort(key=lambda s: (s.match_no or 99, s.scheduled_at))
        return result
