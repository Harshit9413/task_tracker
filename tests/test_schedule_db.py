import json
import os
import sys
import tempfile
import pytest

os.environ["TRACKER_DB_OVERRIDE"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as db


@pytest.fixture(autouse=True)
def fresh_db():
    db.init_db()
    yield
    try:
        os.remove(os.environ["TRACKER_DB_OVERRIDE"])
    except FileNotFoundError:
        pass
    os.environ["TRACKER_DB_OVERRIDE"] = tempfile.mktemp(suffix=".db")


_uid_counter = 0

def _make_team_and_user():
    global _uid_counter
    _uid_counter += 1
    email = f"tl{_uid_counter}@test.com"
    from auth import hash_password
    conn = db._get_conn()
    try:
        cur = conn.execute("INSERT INTO teams (name) VALUES ('TestTeam')")
        team_id = cur.lastrowid
        cur2 = conn.execute(
            "INSERT INTO users (name, email, password_hash, role, team_id) VALUES (?,?,?,?,?)",
            ("Test Leader", email, hash_password("x"), "leader", team_id),
        )
        user_id = cur2.lastrowid
        conn.commit()
    finally:
        conn.close()
    return team_id, user_id


def test_create_and_get_schedule():
    team_id, user_id = _make_team_and_user()
    recipients = json.dumps(["hr@co.com", "cto@co.com"])
    sid = db.create_email_schedule(
        team_id=team_id,
        created_by=user_id,
        label="EOD Report",
        send_time="18:00",
        days="daily",
        recipients_json=recipients,
        auto_cc_team=True,
        content_type="both",
    )
    assert isinstance(sid, int) and sid > 0
    schedules = db.get_team_schedules(team_id)
    assert len(schedules) == 1
    s = schedules[0]
    assert s["label"] == "EOD Report"
    assert s["send_time"] == "18:00"
    assert s["days"] == "daily"
    assert s["auto_cc_team"] == 1
    assert s["content_type"] == "both"
    assert s["is_active"] == 1


def test_get_all_active_schedules():
    team_id, user_id = _make_team_and_user()
    recipients = json.dumps(["x@x.com"])
    db.create_email_schedule(team_id, user_id, "A", "09:00", "weekdays", recipients, True, "updates")
    db.create_email_schedule(team_id, user_id, "B", "17:00", "daily", recipients, False, "mom")
    active = db.get_all_active_schedules()
    assert len(active) == 2


def test_toggle_schedule():
    team_id, user_id = _make_team_and_user()
    sid = db.create_email_schedule(
        team_id, user_id, "X", "10:00", "daily", json.dumps(["a@b.com"]), True, "both"
    )
    db.toggle_schedule(sid, False)
    active = db.get_all_active_schedules()
    assert all(s["id"] != sid for s in active)

    db.toggle_schedule(sid, True)
    active = db.get_all_active_schedules()
    assert any(s["id"] == sid for s in active)


def test_delete_schedule():
    team_id, user_id = _make_team_and_user()
    sid = db.create_email_schedule(
        team_id, user_id, "Del", "12:00", "daily", json.dumps(["z@z.com"]), True, "updates"
    )
    db.delete_schedule(sid)
    schedules = db.get_team_schedules(team_id)
    assert len(schedules) == 0
