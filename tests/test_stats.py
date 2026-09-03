from datetime import timedelta

from app.storage import Storage, utcnow


def test_week_month_stats_and_roi(tmp_path):
    s = Storage(tmp_path / "db.sqlite3")
    now = utcnow()
    s.record_bet("a", "M3-TB4.5", 1, "TB", 1.5, 1000, "HIGH", "A", "B", now - timedelta(days=1))
    s.settle_bet("a", True, now - timedelta(days=1))
    s.record_bet("b", "A+ v4", 2, "TB", 1.4, 1000, "HIGH", "A", "B", now - timedelta(days=2))
    s.settle_bet("b", False, now - timedelta(days=2))
    p = s.performance(7, now=now)
    assert p["n"] == 2 and p["w"] == 1 and p["l"] == 1
    assert abs(p["win_rate"] - 0.5) < 1e-9
    # +500 -1000 = -500 over 2000 turnover = -25%
    assert abs(p["roi"] + 0.25) < 1e-9
    assert "7 дней" in s.stats_text()
    assert "30 дней" in s.stats_text()
    s.close()
