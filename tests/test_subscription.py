from datetime import timedelta

from app.storage import Storage, utcnow


def test_invite_is_one_time_and_grants_subscription(tmp_path):
    s = Storage(tmp_path / "db.sqlite3")
    s.ensure_owner(1)
    token = s.create_invite(7, created_by=1, valid_hours=24)
    ok, _, expiry = s.redeem_invite(token, 22, "user", "Ivan")
    assert ok and expiry is not None
    assert s.has_active_access(22)
    ok2, msg2, _ = s.redeem_invite(token, 23, "other", "Petr")
    assert not ok2 and "использована" in msg2
    s.close()


def test_extension_adds_to_current_expiry(tmp_path):
    s = Storage(tmp_path / "db.sqlite3")
    s.ensure_owner(1)
    token = s.create_invite(7, 1)
    ok, _, first = s.redeem_invite(token, 22, None, "Ivan")
    assert ok and first
    second = s.extend_subscription(22, 14)
    assert second and abs((second - first) - timedelta(days=14)) < timedelta(seconds=2)
    s.close()


def test_expired_user_not_in_recipients(tmp_path):
    s = Storage(tmp_path / "db.sqlite3")
    s.ensure_owner(1)
    s.touch_user(22, None, "Ivan")
    s.conn.execute(
        "UPDATE users SET enabled=1,subscription_expires_at=? WHERE chat_id=22",
        ((utcnow() - timedelta(seconds=1)).isoformat(),),
    )
    s.conn.commit()
    assert 22 not in s.access_recipients()
    assert 1 in s.access_recipients()
    s.close()
