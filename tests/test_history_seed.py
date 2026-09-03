from datetime import datetime, timezone
from pathlib import Path

from app.storage import Storage


def test_september_history_seed_is_idempotent_and_has_expected_stats(tmp_path):
    db = Storage(tmp_path / "db.sqlite3")
    seed = Path(__file__).parents[1] / "seed" / "bet_history_september_2026.json"
    assert db.seed_bets(seed) == 9
    assert db.seed_bets(seed) == 0
    assert db.pending_result_notifications() == []

    now = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
    allp = db.performance(7, now=now)
    assert (allp["n"], allp["w"], allp["l"]) == (9, 3, 6)
    assert abs(allp["pnl"] + 4600) < 1e-9
    assert abs(allp["roi"] - (-4600 / 9000)) < 1e-9

    m3 = db.performance(7, now=now, strategy="M3-TB4.5")
    assert (m3["n"], m3["w"], m3["l"]) == (3, 0, 3)
    assert abs(m3["roi"] + 1.0) < 1e-9

    ap = db.performance(7, now=now, strategy="A+ v4")
    assert (ap["n"], ap["w"], ap["l"]) == (2, 2, 0)
    assert abs(ap["roi"] - 0.4) < 1e-9

    it = db.performance(7, now=now, strategy="IT-L2 v5")
    assert (it["n"], it["w"], it["l"]) == (4, 1, 3)
    assert abs(it["roi"] + 0.6) < 1e-9
    db.close()
