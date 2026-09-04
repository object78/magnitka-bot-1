from datetime import datetime, timezone
from pathlib import Path

from app.storage import Storage


def test_settled_live_bet_waits_for_result_notification(tmp_path: Path):
    st = Storage(tmp_path / "db.sqlite3")
    st.record_bet("k1", "A+ v4", 1, "ТБ0,5 2П", 1.4, 1000, "HIGH", "A", "B", datetime.now(timezone.utc))
    assert st.settle_bet("k1", True, datetime.now(timezone.utc)) is True
    pending = st.pending_result_notifications()
    assert len(pending) == 1
    assert pending[0]["event_key"] == "k1"
    assert pending[0]["result"] == "W"
    st.mark_result_notified("k1")
    assert st.pending_result_notifications() == []


def test_settle_is_idempotent(tmp_path: Path):
    st = Storage(tmp_path / "db.sqlite3")
    st.record_bet("k2", "M3-TB4.5", 2, "ТБ4,5", 1.5, 1000, "HIGH", "A", "B", datetime.now(timezone.utc))
    assert st.settle_bet("k2", False) is True
    assert st.settle_bet("k2", True) is False


def test_upgrade_marks_existing_settled_rows_as_already_notified(tmp_path: Path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE bets(
            event_key TEXT PRIMARY KEY, strategy TEXT NOT NULL, game_id INTEGER NOT NULL,
            placed_at TEXT NOT NULL, market TEXT NOT NULL, min_odds REAL NOT NULL, stake REAL NOT NULL,
            tier TEXT, team1 TEXT, team2 TEXT, result TEXT, pnl REAL, settled_at TEXT
        );
        INSERT INTO bets VALUES('old','M3-TB4.5',1,'2026-09-01T00:00:00+00:00','ТБ4,5',1.5,1000,'HIGH','A','B','L',-1000,'2026-09-01T01:00:00+00:00');
        """
    )
    con.commit(); con.close()
    st = Storage(path)
    assert st.pending_result_notifications() == []


def test_result_message_has_no_money_stats_or_roi():
    from app.monitor import Monitor
    from app.models import GameSnapshot
    m = Monitor.__new__(Monitor)
    bet = {
        "result": "W", "strategy": "A+ v4", "tier": "HIGH",
        "team1": "Хитрые Лисы", "team2": "Свирепые Ежи",
        "market": "ТБ0,5 2П"
    }
    g = GameSnapshot(1, "", None, 5, None, "Хитрые Лисы", "Свирепые Ежи", "", period_scores=[(1,1),(1,0)])
    text = m._result_message(bet, g)
    assert "СТАВКА ПРОШЛА" in text
    assert "ROI" not in text
    assert "₽" not in text
    assert "проход" not in text.lower()


def test_result_message_hides_internal_strategy_name():
    from app.monitor import Monitor
    from app.models import GameSnapshot
    m = Monitor.__new__(Monitor)
    bet = {
        "result": "W", "strategy": "IT-L2 v5", "tier": "ULTRA",
        "team1": "Хитрые Лисы", "team2": "Свирепые Ежи",
        "market": "ИТБ0,5 Хитрых Лис во 2-м периоде"
    }
    g = GameSnapshot(1, "", None, 4, None, "Хитрые Лисы", "Свирепые Ежи", "", period_scores=[(0,3),(1,0)])
    # _trusted_p2_score needs only the snapshot for a finished-period score.
    g.break_after_period = 2
    text = m._result_message(bet, g)
    assert "IT-L2" not in text
    assert "A+" not in text
    assert "M3" not in text
    assert "СТАВКА ПРОШЛА | ULTRA" in text
    assert "Рынок: ИТБ0,5 Хитрых Лис во 2-м периоде" in text
