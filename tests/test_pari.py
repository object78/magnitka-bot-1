from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Config
from app.models import GameSnapshot
from app.monitor import Monitor
from app.pari import PariLiveSnapshot, discover_tournament_url, parse_pari_tournament
from app.storage import Storage


def test_discover_current_magnitka_tournament():
    html = '''<html><body>
    <a href="/live/hockey/country/russia/tournament/4857">Открытый Чемпионат Магнитка Оупен. 3х10. Дневной Турнир №4</a>
    <a href="/live/hockey/country/russia/tournament/4858">Открытый Чемпионат Магнитка Оупен. 3х10. Дневной Турнир №5</a>
    </body></html>'''
    assert discover_tournament_url(html, "https://pari.ru", 5) == "https://pari.ru/live/hockey/country/russia/tournament/4858"


def test_parse_today_like_third_period_card():
    html = '''<html><body><div class="event-card">
      <a href="/live/hockey/country/russia/133535/67764726">Меткие Стрелки - Свирепые Ежи</a>
      <span>3x10</span><span>28:22</span><span>1:1</span><span>(0-0 1-1)</span><span>(…1-1)</span>
    </div></body></html>'''
    rows = parse_pari_tournament(html, "https://pari.ru/live/hockey/country/russia/tournament/4858")
    assert len(rows) == 1
    r = rows[0]
    assert r.total_elapsed_seconds == 28 * 60 + 22
    assert r.period == 3
    assert r.period_elapsed_seconds == 8 * 60 + 22
    assert r.score == (1, 1)
    assert r.p1_score == (0, 0)
    assert r.p2_score == (1, 1)


def test_parse_second_period_derives_live_p2_score():
    html = '''<html><body><div class="event-card">
      <a href="/x">Хитрые Лисы - Свирепые Ежи</a>
      <span>3x10</span><span>10:45</span><span>0:1</span><span>(0-0)</span>
    </div></body></html>'''
    r = parse_pari_tournament(html, "https://pari.ru/t")[0]
    assert r.period == 2
    assert r.period_elapsed_seconds == 45
    assert r.p1_score == (0, 0)
    assert r.p2_score == (0, 1)


class Dummy:
    pass


def _cfg(tmp_path: Path) -> Config:
    return Config(
        telegram_token="x", owner_telegram_id=1, invite_valid_hours=24, flat_stake=1000,
        db_path=tmp_path / "db.sqlite3", seed_path=tmp_path / "none.json", bet_seed_path=tmp_path / "none_bets.json",
        base_url="https://mg-open.org", tournament_tz_name="Asia/Yekaterinburg", user_tz_name=None,
        start_monitor_minutes_before=60, pregame_m3_minutes=10, live_poll_seconds=5,
        idle_poll_seconds=60, schedule_refresh_seconds=180, target_warning_seconds=30,
        send_cancel_messages=True, send_day_start_message=True, request_timeout_seconds=15,
        calendar_candidate_limit=30, p2_default_offset_seconds=900, p2_prep_lead_seconds=60,
        p2_offset_min_seconds=600, p2_offset_max_seconds=1800,
    )


def test_monitor_uses_pari_clock_and_score(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    m = Monitor(_cfg(tmp_path), Dummy(), storage, Dummy())
    g = GameSnapshot(
        game_id=1, url="u", day_no=5, match_no=4,
        scheduled_at=datetime(2026, 9, 4, 12, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg")),
        team1="Хитрые Лисы", team2="Свирепые Ежи", status_text="Новый матч",
    )
    m.pari_rows = [PariLiveSnapshot(
        team1="Хитрые Лисы", team2="Свирепые Ежи", tournament_url="u",
        total_elapsed_seconds=10*60+55, period=2, period_elapsed_seconds=55,
        score=(0, 1), period_scores=[(0, 0), (0, 1)],
    )]
    assert m._period_elapsed(g) == 55
    assert m._trusted_p2_score(g) == (0, 1)
    assert m._fox_p2_goals(g) == 0
    storage.close()


def test_config_default_pari_discovery_is_russia(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.delenv("PARI_DISCOVERY_PATH", raising=False)
    cfg = Config.from_env()
    assert cfg.pari_discovery_path == "/live/hockey/country/russia"


def test_pari_discovery_falls_back_from_old_cyberhockey_path(monkeypatch):
    import asyncio
    from app.pari import PariLiveSource

    src = PariLiveSource(discovery_path="/live/hockey/category/cyberhockey")
    seen = []

    async def fake_get(url: str) -> str:
        seen.append(url)
        if url.endswith("/live/hockey/category/cyberhockey"):
            return '<html><body><a href="/live/hockey/category/cyberhockey/x">NHL 26. H2H</a></body></html>'
        if url.endswith("/live/hockey/country/russia"):
            return '<html><body><a href="/live/hockey/country/russia/tournament/4858">Открытый Чемпионат Магнитка Оупен. 3х10. Дневной Турнир №5</a></body></html>'
        raise AssertionError(url)

    monkeypatch.setattr(src, "_get", fake_get)
    try:
        url = asyncio.run(src.discover(day_no=5, force=True))
        assert url == "https://pari.ru/live/hockey/country/russia/tournament/4858"
        assert any(u.endswith("/live/hockey/country/russia") for u in seen)
    finally:
        asyncio.run(src.close())
