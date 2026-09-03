from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
            CREATE TABLE IF NOT EXISTS users(
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                enabled INTEGER NOT NULL DEFAULT 1,
                subscription_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites(
                token TEXT PRIMARY KEY,
                duration_days INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                link_expires_at TEXT NOT NULL,
                used_by INTEGER,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS subscription_notices(
                notice_key TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications(
                event_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bets(
                event_key TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                game_id INTEGER NOT NULL,
                placed_at TEXT NOT NULL,
                market TEXT NOT NULL,
                min_odds REAL NOT NULL,
                stake REAL NOT NULL,
                tier TEXT,
                team1 TEXT,
                team2 TEXT,
                result TEXT,
                pnl REAL,
                settled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bets_placed_at ON bets(placed_at);
            CREATE INDEX IF NOT EXISTS idx_users_expiry ON users(subscription_expires_at);
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
        # Lightweight schema migration for automatic result notifications.
        bet_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(bets)")}
        if "result_notified" not in bet_cols:
            self.conn.execute("ALTER TABLE bets ADD COLUMN result_notified INTEGER NOT NULL DEFAULT 0")
            # Existing settled rows came from older versions/history seeds. Never spam their old results after upgrade.
            self.conn.execute("UPDATE bets SET result_notified=1 WHERE result IN ('W','L')")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- Paid access ----------
    def ensure_owner(self, chat_id: int, username: str | None = None, first_name: str | None = None) -> None:
        now = utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO users(chat_id,username,first_name,role,enabled,subscription_expires_at,created_at,updated_at) "
            "VALUES(?,?,?,'owner',1,NULL,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET role='owner',enabled=1,username=COALESCE(excluded.username,users.username),"
            "first_name=COALESCE(excluded.first_name,users.first_name),updated_at=excluded.updated_at",
            (chat_id, username, first_name, now, now),
        )
        self.conn.commit()

    def owner_ids(self) -> list[int]:
        return [int(r[0]) for r in self.conn.execute("SELECT chat_id FROM users WHERE role='owner' AND enabled=1")]

    def is_owner(self, chat_id: int) -> bool:
        # Owner keeps admin rights even if signal delivery was muted.
        row = self.conn.execute("SELECT 1 FROM users WHERE chat_id=? AND role='owner'", (chat_id,)).fetchone()
        return row is not None

    def touch_user(self, chat_id: int, username: str | None, first_name: str | None) -> None:
        now = utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO users(chat_id,username,first_name,role,enabled,subscription_expires_at,created_at,updated_at) "
            "VALUES(?,?,?,'user',0,NULL,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,updated_at=excluded.updated_at",
            (chat_id, username, first_name, now, now),
        )
        self.conn.commit()

    def get_user(self, chat_id: int):
        return self.conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()

    def has_active_access(self, chat_id: int, now: datetime | None = None) -> bool:
        row = self.get_user(chat_id)
        if not row or not int(row["enabled"]):
            return False
        if row["role"] == "owner":
            return True
        expiry = _parse_dt(row["subscription_expires_at"])
        now = (now or utcnow()).astimezone(timezone.utc)
        return bool(expiry and expiry > now)

    def access_recipients(self, now: datetime | None = None) -> list[int]:
        now = (now or utcnow()).astimezone(timezone.utc)
        out: list[int] = []
        for r in self.conn.execute("SELECT chat_id,role,enabled,subscription_expires_at FROM users WHERE enabled=1"):
            if r["role"] == "owner":
                out.append(int(r["chat_id"]))
                continue
            expiry = _parse_dt(r["subscription_expires_at"])
            if expiry and expiry > now:
                out.append(int(r["chat_id"]))
        return out

    # Backwards-compatible name used by TelegramGateway.
    def subscribers(self) -> list[int]:
        return self.access_recipients()

    def add_subscriber(self, chat_id: int) -> None:
        # Legacy compatibility only. Public /start no longer grants access.
        self.touch_user(chat_id, None, None)

    def set_subscriber_enabled(self, chat_id: int, enabled: bool) -> None:
        now = utcnow().isoformat()
        self.conn.execute("UPDATE users SET enabled=?,updated_at=? WHERE chat_id=?", (int(enabled), now, chat_id))
        self.conn.commit()

    def create_invite(self, duration_days: int, created_by: int, valid_hours: int = 24) -> str:
        if duration_days not in (7, 14, 30):
            raise ValueError("duration_days must be 7, 14 or 30")
        token = secrets.token_urlsafe(18)
        now = utcnow()
        self.conn.execute(
            "INSERT INTO invites(token,duration_days,created_by,created_at,link_expires_at) VALUES(?,?,?,?,?)",
            (token, duration_days, created_by, now.isoformat(), (now + timedelta(hours=valid_hours)).isoformat()),
        )
        self.conn.commit()
        return token

    def redeem_invite(self, token: str, chat_id: int, username: str | None, first_name: str | None) -> tuple[bool, str, datetime | None]:
        now = utcnow()
        row = self.conn.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
        if not row:
            return False, "Приглашение не найдено.", None
        if row["used_by"] is not None:
            return False, "Эта ссылка уже использована.", None
        link_exp = _parse_dt(row["link_expires_at"])
        if not link_exp or link_exp <= now:
            return False, "Срок действия ссылки истёк. Попросите новую ссылку у владельца.", None

        current = self.get_user(chat_id)
        old_exp = _parse_dt(current["subscription_expires_at"]) if current else None
        base = old_exp if old_exp and old_exp > now else now
        new_exp = base + timedelta(days=int(row["duration_days"]))
        created = current["created_at"] if current else now.isoformat()
        self.conn.execute(
            "INSERT INTO users(chat_id,username,first_name,role,enabled,subscription_expires_at,created_at,updated_at) "
            "VALUES(?,?,?,'user',1,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,"
            "enabled=1,subscription_expires_at=excluded.subscription_expires_at,updated_at=excluded.updated_at",
            (chat_id, username, first_name, new_exp.isoformat(), created, now.isoformat()),
        )
        self.conn.execute("UPDATE invites SET used_by=?,used_at=? WHERE token=?", (chat_id, now.isoformat(), token))
        self.conn.commit()
        return True, f"Доступ активирован на {int(row['duration_days'])} дней.", new_exp

    def extend_subscription(self, chat_id: int, duration_days: int) -> datetime | None:
        if duration_days not in (7, 14, 30):
            raise ValueError("duration_days must be 7, 14 or 30")
        row = self.get_user(chat_id)
        if not row or row["role"] == "owner":
            return None
        now = utcnow()
        old_exp = _parse_dt(row["subscription_expires_at"])
        base = old_exp if old_exp and old_exp > now else now
        new_exp = base + timedelta(days=duration_days)
        self.conn.execute(
            "UPDATE users SET enabled=1,subscription_expires_at=?,updated_at=? WHERE chat_id=?",
            (new_exp.isoformat(), now.isoformat(), chat_id),
        )
        self.conn.commit()
        return new_exp

    def revoke_subscription(self, chat_id: int) -> bool:
        row = self.get_user(chat_id)
        if not row or row["role"] == "owner":
            return False
        self.conn.execute("UPDATE users SET enabled=0,updated_at=? WHERE chat_id=?", (utcnow().isoformat(), chat_id))
        self.conn.commit()
        return True

    def list_users(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT chat_id,username,first_name,role,enabled,subscription_expires_at,created_at FROM users ORDER BY role DESC,created_at DESC"
        ))

    def subscription_info(self, chat_id: int) -> dict | None:
        row = self.get_user(chat_id)
        if not row:
            return None
        expiry = _parse_dt(row["subscription_expires_at"])
        return {
            "chat_id": int(row["chat_id"]),
            "username": row["username"],
            "first_name": row["first_name"],
            "role": row["role"],
            "enabled": bool(row["enabled"]),
            "expires_at": expiry,
            "active": self.has_active_access(chat_id),
        }

    def notice_sent(self, notice_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM subscription_notices WHERE notice_key=?", (notice_key,)).fetchone() is not None

    def mark_notice_sent(self, notice_key: str, chat_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO subscription_notices(notice_key,chat_id,created_at) VALUES(?,?,?)",
            (notice_key, chat_id, utcnow().isoformat()),
        )
        self.conn.commit()

    def expiring_users(self, hours_from: int, hours_to: int) -> list[sqlite3.Row]:
        now = utcnow()
        lo = now + timedelta(hours=hours_from)
        hi = now + timedelta(hours=hours_to)
        return list(self.conn.execute(
            "SELECT * FROM users WHERE role='user' AND enabled=1 AND subscription_expires_at>? AND subscription_expires_at<=?",
            (lo.isoformat(), hi.isoformat()),
        ))

    def expired_enabled_users(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM users WHERE role='user' AND enabled=1 AND subscription_expires_at IS NOT NULL AND subscription_expires_at<=?",
            (utcnow().isoformat(),),
        ))

    # ---------- Bet performance ----------
    def record_bet(
        self, event_key: str, strategy: str, game_id: int, market: str, min_odds: float,
        stake: float, tier: str | None, team1: str | None, team2: str | None,
        placed_at: datetime | None = None,
    ) -> None:
        placed = (placed_at or utcnow()).astimezone(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO bets(event_key,strategy,game_id,placed_at,market,min_odds,stake,tier,team1,team2) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_key, strategy, game_id, placed, market, float(min_odds), float(stake), tier, team1, team2),
        )
        self.conn.commit()

    def open_bets(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM bets WHERE result IS NULL ORDER BY placed_at"))

    def settle_bet(self, event_key: str, won: bool, settled_at: datetime | None = None) -> bool:
        row = self.conn.execute("SELECT min_odds,stake,result FROM bets WHERE event_key=?", (event_key,)).fetchone()
        if not row or row["result"] is not None:
            return False
        # Internal unit P/L is kept only to calculate ROI. It is never shown to users.
        pnl = float(row["stake"]) * (float(row["min_odds"]) - 1.0) if won else -float(row["stake"])
        self.conn.execute(
            "UPDATE bets SET result=?,pnl=?,settled_at=? WHERE event_key=?",
            ("W" if won else "L", pnl, (settled_at or utcnow()).astimezone(timezone.utc).isoformat(), event_key),
        )
        self.conn.commit()
        return True

    def pending_result_notifications(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM bets WHERE result IN ('W','L') AND COALESCE(result_notified,0)=0 ORDER BY settled_at, placed_at"
            )
        )

    def mark_result_notified(self, event_key: str) -> None:
        self.conn.execute("UPDATE bets SET result_notified=1 WHERE event_key=?", (event_key,))
        self.conn.commit()

    def performance(self, days: int, now: datetime | None = None, strategy: str | None = None) -> dict:
        now = (now or utcnow()).astimezone(timezone.utc)
        since = now - timedelta(days=days)
        sql = "SELECT result,pnl,stake FROM bets WHERE result IN ('W','L') AND placed_at>=? AND placed_at<=?"
        params: list = [since.isoformat(), now.isoformat()]
        if strategy:
            sql += " AND strategy=?"
            params.append(strategy)
        rows = list(self.conn.execute(sql, params))
        n = len(rows)
        w = sum(r["result"] == "W" for r in rows)
        l = n - w
        pnl = sum(float(r["pnl"] or 0) for r in rows)
        turnover = sum(float(r["stake"] or 0) for r in rows)
        return {
            "days": days,
            "n": n,
            "w": w,
            "l": l,
            "win_rate": (w / n) if n else None,
            "pnl": pnl,
            "roi": (pnl / turnover) if turnover else None,
        }

    def stats_text(self) -> str:
        def line(label: str, p: dict) -> str:
            if not p["n"]:
                return f"{label}: пока нет завершённых ставок"
            return (
                f"{label}: {p['w']}–{p['l']} | проход {p['win_rate']*100:.1f}% | "
                f"ROI {p['roi']*100:+.1f}%"
            )

        names = [("M3", "M3-TB4.5"), ("A+", "A+ v4"), ("IT-L2", "IT-L2 v5")]
        blocks = ["📊 Статистика сигналов"]
        for days, title in ((7, "Последние 7 дней"), (30, "Последние 30 дней")):
            blocks.append("\n" + title)
            blocks.append(line("ВСЕ", self.performance(days)))
            for short, strategy in names:
                blocks.append(line(short, self.performance(days, strategy=strategy)))
        blocks.append("\nROI рассчитан по минимальному коэффициенту сигнала. Размер вашей ставки на процент ROI не влияет.")
        return "\n".join(blocks)

    def seed_bets(self, seed_path: Path) -> int:
        """Import confirmed historical bets once using their real event keys.

        INSERT OR IGNORE makes the import idempotent and prevents duplication if a
        signal with the same event_key is already present in the persistent DB.
        """
        if not seed_path.exists():
            return 0
        data = json.loads(seed_path.read_text(encoding="utf-8"))
        inserted = 0
        for item in data.get("bets", []):
            result = str(item.get("result", "")).upper()
            if result not in {"W", "L"}:
                continue
            placed = _parse_dt(item.get("placed_at"))
            settled = _parse_dt(item.get("settled_at"))
            if not placed:
                continue
            stake = float(item.get("stake", 1000))
            odds = float(item["min_odds"])
            pnl = stake * (odds - 1.0) if result == "W" else -stake
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO bets(event_key,strategy,game_id,placed_at,market,min_odds,stake,tier,team1,team2,result,pnl,settled_at,result_notified) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    str(item["event_key"]), str(item["strategy"]), int(item["game_id"]),
                    placed.isoformat(), str(item["market"]), odds, stake, item.get("tier"),
                    item.get("team1"), item.get("team2"), result, pnl,
                    (settled or placed).isoformat(),
                ),
            )
            inserted += int(cur.rowcount or 0)
        self.conn.commit()
        return inserted

    # ---------- Existing notification / model state ----------
    def notified(self, event_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notifications WHERE event_key=?", (event_key,)).fetchone() is not None

    def mark_notified(self, event_key: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notifications(event_key,created_at) VALUES(?,?)",
            (event_key, utcnow().isoformat()),
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

    # ---------- Goalie history ----------
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
