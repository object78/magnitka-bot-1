"""Manual live-page diagnostic: print what the parser sees for one game URL."""
from __future__ import annotations

import asyncio
import os
import sys
from zoneinfo import ZoneInfo

from app.source import MagnitkaSource


async def main(url: str) -> None:
    base = os.getenv("MG_OPEN_BASE_URL", "https://mg-open.org")
    src = MagnitkaSource(base, ZoneInfo(os.getenv("TOURNAMENT_TZ", "Asia/Yekaterinburg")))
    try:
        s = await src.fetch_game(url)
        print("game_id:", s.game_id)
        print("match_no:", s.match_no)
        print("scheduled_at:", s.scheduled_at)
        print("teams:", s.team1, "—", s.team2)
        print("status:", s.status_text)
        print("live_period:", s.live_period)
        print("live_elapsed_seconds:", s.live_elapsed_seconds)
        print("break_after_period:", s.break_after_period)
        print("period_scores:", s.period_scores)
        print("p2_score:", s.p2_score())
        print("events:", len(s.events))
        print("rosters:", {k: len(v) for k, v in s.rosters.items()})
    finally:
        await src.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/inspect_page.py https://mg-open.org/game/1/5122.html")
    asyncio.run(main(sys.argv[1]))
