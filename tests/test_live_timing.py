from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Config
from app.models import GameSnapshot, GoalEvent
from app.monitor import Monitor
from app.storage import Storage

TZ = ZoneInfo("Asia/Yekaterinburg")


def cfg(tmp_path: Path) -> Config:
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


class Dummy:
    pass


def snapshot(events=None):
    return GameSnapshot(
        game_id=5129, url="https://mg-open.org/game/1/5129.html", day_no=4, match_no=4,
        scheduled_at=datetime(2026, 9, 3, 12, 0, tzinfo=TZ),
        team1="Хитрые Лисы", team2="Свирепые Ежи", status_text="Новый матч",
        events=events or [],
    )


def test_new_p2_event_anchors_period_start(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    m = Monitor(cfg(tmp_path), Dummy(), storage, Dummy())
    old = snapshot([])
    ev = GoalEvent(period=2, raw_time="10:29", absolute_seconds=629, score_after="0:4", team="Свирепые Ежи")
    new = snapshot([ev])
    observed = datetime(2026, 9, 3, 12, 16, 29, tzinfo=TZ)
    m._learn_live_timing(old, new, observed)
    start, source = m._p2_start_info(new)
    assert start == datetime(2026, 9, 3, 12, 16, 0, tzinfo=TZ)
    assert source == "new-p2-event"
    storage.close()


def test_manual_prep_fallback_10_seconds_before_estimated_p2(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    m = Monitor(cfg(tmp_path), Dummy(), storage, Dummy())
    g = snapshot([])
    # No PARI row, no mg-open live timer. Default P2 start is scheduled + 15 min.
    m.now = lambda: datetime(2026, 9, 3, 12, 14, 50, tzinfo=TZ)
    assert m._near_p2_start(g) is True
    assert "расчётное начало 2П" in m._prep_clock_source(g)
    # Safety PREP does not create a fake live clock for a bet decision.
    assert m._period_elapsed(g) is None
    storage.close()


def test_manual_prep_fallback_not_too_early(tmp_path):
    storage = Storage(tmp_path / "db.sqlite3")
    m = Monitor(cfg(tmp_path), Dummy(), storage, Dummy())
    g = snapshot([])
    m.now = lambda: datetime(2026, 9, 3, 12, 14, 40, tzinfo=TZ)
    assert m._near_p2_start(g) is False
    storage.close()
