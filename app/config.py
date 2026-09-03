from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    owner_telegram_id: int | None
    invite_valid_hours: int
    flat_stake: float
    db_path: Path
    seed_path: Path
    bet_seed_path: Path
    base_url: str
    tournament_tz_name: str
    user_tz_name: str | None
    start_monitor_minutes_before: int
    pregame_m3_minutes: int
    live_poll_seconds: float
    idle_poll_seconds: float
    schedule_refresh_seconds: float
    target_warning_seconds: int
    send_cancel_messages: bool
    send_day_start_message: bool
    request_timeout_seconds: float
    calendar_candidate_limit: int
    p2_default_offset_seconds: int
    p2_prep_lead_seconds: int
    p2_offset_min_seconds: int
    p2_offset_max_seconds: int

    @property
    def tournament_tz(self) -> ZoneInfo:
        return ZoneInfo(self.tournament_tz_name)

    @property
    def user_tz(self) -> ZoneInfo | None:
        return ZoneInfo(self.user_tz_name) if self.user_tz_name else None

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")
        owner_raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
        return cls(
            telegram_token=token,
            owner_telegram_id=int(owner_raw) if owner_raw else None,
            invite_valid_hours=int(os.getenv("INVITE_VALID_HOURS", "24")),
            flat_stake=float(os.getenv("FLAT_STAKE", "1000")),
            db_path=Path(os.getenv("DB_PATH", "data/magnitka_bot.sqlite3")),
            seed_path=Path(os.getenv("GOALIE_SEED_PATH", "seed/goalie_seed_august_2026.json")),
            bet_seed_path=Path(os.getenv("BET_HISTORY_SEED_PATH", "seed/bet_history_september_2026.json")),
            base_url=os.getenv("MG_OPEN_BASE_URL", "https://mg-open.org").rstrip("/"),
            tournament_tz_name=os.getenv("TOURNAMENT_TZ", "Asia/Yekaterinburg"),
            user_tz_name=os.getenv("USER_TZ") or None,
            start_monitor_minutes_before=int(os.getenv("START_MONITOR_MINUTES_BEFORE", "60")),
            pregame_m3_minutes=int(os.getenv("M3_PREGAME_MINUTES", "10")),
            live_poll_seconds=float(os.getenv("LIVE_POLL_SECONDS", "5")),
            idle_poll_seconds=float(os.getenv("IDLE_POLL_SECONDS", "60")),
            schedule_refresh_seconds=float(os.getenv("SCHEDULE_REFRESH_SECONDS", "180")),
            target_warning_seconds=int(os.getenv("TARGET_WARNING_SECONDS", "30")),
            send_cancel_messages=_bool("SEND_CANCEL_MESSAGES", True),
            send_day_start_message=_bool("SEND_DAY_START_MESSAGE", True),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15")),
            calendar_candidate_limit=int(os.getenv("CALENDAR_CANDIDATE_LIMIT", "30")),
            p2_default_offset_seconds=int(os.getenv("P2_DEFAULT_OFFSET_SECONDS", "900")),
            p2_prep_lead_seconds=int(os.getenv("P2_PREP_LEAD_SECONDS", "60")),
            p2_offset_min_seconds=int(os.getenv("P2_OFFSET_MIN_SECONDS", "600")),
            p2_offset_max_seconds=int(os.getenv("P2_OFFSET_MAX_SECONDS", "1800")),
        )
