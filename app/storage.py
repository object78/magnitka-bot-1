from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS subscribers(
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications(
                event_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS goalie_history(
                goalie TEXT PRIMARY KEY,
                days INTEGER NOT NULL,
                ga2 REAL NOT NULL,
                ga REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_team_days(
                day TEXT NOT NULL,
                team TEXT NOT NULL,
                PRIMARY KEY(day, team)
            );
            CREATE TABLE IF NOT EXISTS state(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def add_subscriber(self, chat_id: int) -> None:
        self.conn.execute(
            "INSERT INTO subscribers(chat_id,enabled,created_at) VALUES(?,1,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=1",
            (chat_id, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def set_subscriber_enabled(self, chat_id: int, enabled: bool) -> None:
        self.conn.execute(
            "INSERT INTO subscribers(chat_id,enabled,created_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
            (chat_id, int(enabled), datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def subscribers(self) -> list[int]:
        return [int(r[0]) for r in self.conn.execute("SELECT chat_id FROM subscribers WHERE enabled=1")]

    def notified(self, event_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notifications WHERE event_key=?", (event_key,)).fetchone() is not None

    def mark_notified(self, event_key: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notifications(event_key,created_at) VALUES(?,?)",
            (event_key, datetime.utcnow().isoformat()),
        )
        self.conn.commit()

    def get_state(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.conn.commit()

    def seed_goalies(self, seed_path: Path) -> None:
        count = self.conn.execute("SELECT COUNT(*) FROM goalie_history").fetchone()[0]
        if count:
            return
        if not seed_path.exists():
            return
        data = json.loads(seed_path.read_text(encoding="utf-8"))
        rows = [(name, int(v["days"]), float(v["ga2"]), float(v["ga"])) for name, v in data["goalies"].items()]
        self.conn.executemany("INSERT INTO goalie_history(goalie,days,ga2,ga) VALUES(?,?,?,?)", rows)
        self.conn.commit()

    def goalie_stats(self, goalie: str) -> tuple[int, float, float]:
        row = self.conn.execute("SELECT days,ga2,ga FROM goalie_history WHERE goalie=?", (goalie,)).fetchone()
        if not row:
            return 0, 0.0, 0.0
        return int(row[0]), float(row[1]), float(row[2])

    def goalie_pair_proxy(self, goalies: list[str]) -> float | None:
        if not goalies:
            return None
        vals = []
        for goalie in goalies:
            days, ga2, _ = self.goalie_stats(goalie)
            # Same smoothing used in the August research: 3 pseudo-days at 2.7 P2 GA/team-day.
            vals.append((ga2 + 3 * 2.7) / (days + 3))
        return sum(vals) / len(vals)

    def processed_team_day(self, day: str, team: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM processed_team_days WHERE day=? AND team=?", (day, team)
        ).fetchone() is not None

    def add_team_day_goalie_result(self, day: str, team: str, goalies: list[str], ga2: int, ga: int) -> None:
        if self.processed_team_day(day, team):
            return
        for goalie in goalies:
            days, old_ga2, old_ga = self.goalie_stats(goalie)
            self.conn.execute(
                "INSERT INTO goalie_history(goalie,days,ga2,ga) VALUES(?,?,?,?) "
                "ON CONFLICT(goalie) DO UPDATE SET days=excluded.days,ga2=excluded.ga2,ga=excluded.ga",
                (goalie, days + 1, old_ga2 + ga2, old_ga + ga),
            )
        self.conn.execute("INSERT INTO processed_team_days(day,team) VALUES(?,?)", (day, team))
        self.conn.commit()
