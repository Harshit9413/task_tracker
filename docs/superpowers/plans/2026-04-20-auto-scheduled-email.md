# Auto-Scheduled Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow team leaders to create multiple scheduled email jobs that automatically send daily updates and/or meeting notes to specified recipients at a configured time each day.

**Architecture:** Three-part change — (1) new `email_schedules` SQLite table + CRUD functions in `database.py`, (2) new "Scheduled Emails" UI page in `app.py`, (3) standalone `scheduler.py` process that polls DB every 60 seconds and fires emails when schedule time matches current time.

**Tech Stack:** Python 3.11, SQLite (sqlite3), Streamlit, smtplib (existing email_utils.py), standard library only (no new dependencies — uses `time.sleep` loop instead of APScheduler)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `database.py` | Modify | Add `email_schedules` table to `_SCHEMA_SQL` + 5 CRUD functions |
| `app.py` | Modify | Add `show_scheduled_emails()` page + sidebar routing for leaders |
| `scheduler.py` | Create | Standalone process: poll DB, match schedule time, build+send email |
| `tests/test_schedule_db.py` | Create | Unit tests for all 5 database CRUD functions |

---

## Task 1: Add `email_schedules` table to database.py

**Files:**
- Modify: `database.py` (lines 31–75, `_SCHEMA_SQL` string)
- Create: `tests/test_schedule_db.py`

### Step 1.1 — Create tests directory and write failing tests

- [ ] Create `tests/__init__.py` (empty file) and `tests/test_schedule_db.py`:

```python
# tests/test_schedule_db.py
import json
import os
import sys
import tempfile
import pytest

# Point database module to a temp file so tests don't touch tracker.db
os.environ["TRACKER_DB_OVERRIDE"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database as db

@pytest.fixture(autouse=True)
def fresh_db():
    # Re-init DB for each test
    db.init_db()
    yield
    # Cleanup
    try:
        os.remove(os.environ["TRACKER_DB_OVERRIDE"])
    except FileNotFoundError:
        pass
    os.environ["TRACKER_DB_OVERRIDE"] = tempfile.mktemp(suffix=".db")


def _make_team_and_user():
    """Helper: insert a team and a leader user, return (team_id, user_id)."""
    conn = db._get_conn()
    cur = conn.execute("INSERT INTO teams (name) VALUES ('TestTeam')")
    team_id = cur.lastrowid
    from auth import hash_password
    cur2 = conn.execute(
        "INSERT INTO users (name, email, password_hash, role, team_id) VALUES (?,?,?,?,?)",
        ("Test Leader", "tl@test.com", hash_password("x"), "leader", team_id),
    )
    user_id = cur2.lastrowid
    conn.commit()
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
    db.create_email_schedule(team_id, user_id, "B", "17:00", "daily",    recipients, False, "mom")
    active = db.get_all_active_schedules()
    assert len(active) == 2


def test_toggle_schedule():
    team_id, user_id = _make_team_and_user()
    sid = db.create_email_schedule(team_id, user_id, "X", "10:00", "daily",
                                    json.dumps(["a@b.com"]), True, "both")
    db.toggle_schedule(sid, False)
    active = db.get_all_active_schedules()
    assert all(s["id"] != sid for s in active)

    db.toggle_schedule(sid, True)
    active = db.get_all_active_schedules()
    assert any(s["id"] == sid for s in active)


def test_delete_schedule():
    team_id, user_id = _make_team_and_user()
    sid = db.create_email_schedule(team_id, user_id, "Del", "12:00", "daily",
                                    json.dumps(["z@z.com"]), True, "updates")
    db.delete_schedule(sid)
    schedules = db.get_team_schedules(team_id)
    assert len(schedules) == 0
```

### Step 1.2 — Run tests to confirm they fail

- [ ] Run: `cd /Users/vishaljangid/learning/harshit/mcp/task_tracker && python -m pytest tests/test_schedule_db.py -v 2>&1 | head -40`

Expected: `AttributeError` or `ImportError` — `create_email_schedule` not found.

### Step 1.3 — Add `TRACKER_DB_OVERRIDE` support to `database.py`

- [ ] In `database.py`, replace the `DB_PATH` line:

```python
# Before:
DB_PATH = Path(__file__).parent / "tracker.db"

# After:
import os as _os
DB_PATH = Path(_os.environ.get("TRACKER_DB_OVERRIDE") or (Path(__file__).parent / "tracker.db"))
```

### Step 1.4 — Add `email_schedules` table to `_SCHEMA_SQL` in `database.py`

- [ ] Append this block inside `_SCHEMA_SQL` (before the closing `"""`):

```sql

CREATE TABLE IF NOT EXISTS email_schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       INTEGER NOT NULL REFERENCES teams(id),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    label         TEXT NOT NULL,
    send_time     TEXT NOT NULL,
    days          TEXT NOT NULL,
    recipients    TEXT NOT NULL,
    auto_cc_team  INTEGER DEFAULT 1,
    content_type  TEXT DEFAULT 'both',
    is_active     INTEGER DEFAULT 1,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Step 1.5 — Add 5 CRUD functions to `database.py`

- [ ] Add after the last function in `database.py` (end of file):

```python
# ---------------------------------------------------------------------------
# Email Schedules
# ---------------------------------------------------------------------------

def create_email_schedule(
    team_id: int,
    created_by: int,
    label: str,
    send_time: str,
    days: str,
    recipients_json: str,
    auto_cc_team: bool,
    content_type: str,
) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO email_schedules
               (team_id, created_by, label, send_time, days, recipients, auto_cc_team, content_type)
               VALUES (?,?,?,?,?,?,?,?)""",
            (team_id, created_by, label, send_time, days, recipients_json,
             int(auto_cc_team), content_type),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_team_schedules(team_id: int) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM email_schedules WHERE team_id=? ORDER BY created_at DESC",
            (team_id,),
        ).fetchall()
    finally:
        conn.close()


def get_all_active_schedules() -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM email_schedules WHERE is_active=1"
        ).fetchall()
    finally:
        conn.close()


def toggle_schedule(schedule_id: int, is_active: bool) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE email_schedules SET is_active=? WHERE id=?",
            (int(is_active), schedule_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_schedule(schedule_id: int) -> None:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM email_schedules WHERE id=?", (schedule_id,))
        conn.commit()
    finally:
        conn.close()
```

### Step 1.6 — Run tests to confirm they pass

- [ ] Run: `python -m pytest tests/test_schedule_db.py -v`

Expected output:
```
tests/test_schedule_db.py::test_create_and_get_schedule PASSED
tests/test_schedule_db.py::test_get_all_active_schedules PASSED
tests/test_schedule_db.py::test_toggle_schedule PASSED
tests/test_schedule_db.py::test_delete_schedule PASSED
4 passed
```

### Step 1.7 — Commit

- [ ] Run:
```bash
git add database.py tests/__init__.py tests/test_schedule_db.py
git commit -m "feat: add email_schedules table and CRUD functions"
```

---

## Task 2: Add "Scheduled Emails" UI page to app.py

**Files:**
- Modify: `app.py`

This page has no server-side logic to unit-test (it's all Streamlit widgets), so we verify manually by running the app.

### Step 2.1 — Add database imports to app.py

- [ ] In `app.py`, find the `from database import (` block and add these three imports:

```python
    get_team_schedules,
    create_email_schedule,
    toggle_schedule,
    delete_schedule,
```

### Step 2.2 — Add `show_scheduled_emails()` function to app.py

- [ ] Add this function after `show_team_settings()` and before `def main():`:

```python
# ---------------------------------------------------------------------------
# Page: Scheduled Emails (Leader only)
# ---------------------------------------------------------------------------

def show_scheduled_emails():
    import json

    st.header("Scheduled Emails")
    user = get_current_user()
    team_id = user["user_team_id"]

    if not team_id:
        st.error("You are not assigned to any team.")
        return

    schedules = get_team_schedules(team_id)

    # ── Existing schedules ────────────────────────────────────────────────────
    if schedules:
        st.subheader("Active Schedules")
        for s in schedules:
            recipients = json.loads(s["recipients"])
            days_label = "Daily" if s["days"] == "daily" else "Weekdays (Mon–Fri)"
            content_label = {"updates": "Updates only", "mom": "MoM only", "both": "Updates + MoM"}.get(
                s["content_type"], s["content_type"]
            )
            cc_label = "Yes" if s["auto_cc_team"] else "No"
            active = bool(s["is_active"])

            with st.container():
                col1, col2, col3 = st.columns([5, 1, 1])
                with col1:
                    st.markdown(
                        f"**{s['label']}** &nbsp; `{s['send_time']}` &nbsp; `{days_label}` &nbsp; `{content_label}`"
                    )
                    st.caption(
                        f"Recipients: {', '.join(recipients) or '(none)'} &nbsp;|&nbsp; Auto-CC team: {cc_label}"
                    )
                with col2:
                    toggle_val = st.toggle(
                        "On", value=active, key=f"toggle_{s['id']}"
                    )
                    if toggle_val != active:
                        toggle_schedule(s["id"], toggle_val)
                        st.rerun()
                with col3:
                    if st.button("Delete", key=f"del_{s['id']}", type="secondary"):
                        delete_schedule(s["id"])
                        st.rerun()
                st.divider()
    else:
        st.info("No scheduled emails yet. Add one below.")

    # ── Add new schedule form ─────────────────────────────────────────────────
    with st.expander("Add New Schedule", expanded=len(schedules) == 0):
        label = st.text_input("Label (e.g. 'Evening Report')", key="sched_label")
        send_time = st.text_input("Send time (HH:MM, 24-hour)", value="18:00", key="sched_time")
        days = st.radio(
            "Repeat on", ["Daily", "Weekdays (Mon–Fri)"],
            key="sched_days", horizontal=True
        )
        content_type = st.radio(
            "Content to include",
            ["Updates + Meeting Notes", "Updates only", "Meeting Notes only"],
            key="sched_content", horizontal=True
        )
        auto_cc = st.checkbox("Auto-CC all team members", value=True, key="sched_auto_cc")
        recipients_raw = st.text_area(
            "Additional recipient emails (comma-separated)",
            placeholder="manager@company.com, hr@company.com",
            key="sched_recipients",
        )

        if st.button("Save Schedule", use_container_width=True, key="sched_save"):
            import re as _re
            # Validate time
            if not _re.match(r"^\d{2}:\d{2}$", send_time.strip()):
                st.error("Time must be in HH:MM format (e.g. 18:00).")
            elif not label.strip():
                st.error("Label cannot be empty.")
            else:
                raw_emails = [e.strip() for e in recipients_raw.split(",") if e.strip()]
                invalid = [e for e in raw_emails if not _re.match(r"[^@]+@[^@]+\.[^@]+", e)]
                if invalid:
                    st.error(f"Invalid emails: {', '.join(invalid)}")
                else:
                    days_val = "daily" if days == "Daily" else "weekdays"
                    ct_map = {
                        "Updates + Meeting Notes": "both",
                        "Updates only": "updates",
                        "Meeting Notes only": "mom",
                    }
                    create_email_schedule(
                        team_id=team_id,
                        created_by=user["user_id"],
                        label=label.strip(),
                        send_time=send_time.strip(),
                        days=days_val,
                        recipients_json=json.dumps(raw_emails),
                        auto_cc_team=auto_cc,
                        content_type=ct_map[content_type],
                    )
                    st.success(f"Schedule '{label.strip()}' saved.")
                    st.rerun()
```

### Step 2.3 — Add page to leader's sidebar and routing in main()

- [ ] In `app.py`, find the `elif role == "leader":` block and add `"Scheduled Emails"`:

```python
# Before:
        elif role == "leader":
            pages = [
                "Add Update",
                "My Updates",
                "Team View",
                "Meeting Notes",
                "Chatbot",
                "Team Settings",
            ]

# After:
        elif role == "leader":
            pages = [
                "Add Update",
                "My Updates",
                "Team View",
                "Meeting Notes",
                "Chatbot",
                "Team Settings",
                "Scheduled Emails",
            ]
```

- [ ] In `app.py`, find the routing block at the bottom of `main()` and add:

```python
# Add after the last elif:
    elif page == "Scheduled Emails":
        show_scheduled_emails()
```

### Step 2.4 — Manually verify UI

- [ ] Run: `streamlit run app.py`
- [ ] Log in as `alice@example.com` / `password123`
- [ ] Click "Scheduled Emails" in sidebar
- [ ] Verify: empty state shows "No scheduled emails yet"
- [ ] Add a schedule: Label="Test", Time="18:00", Daily, Both, no auto-CC, recipient="test@test.com"
- [ ] Verify: schedule appears in list with correct label, time, days
- [ ] Toggle it off → verify it goes inactive (toggle turns grey)
- [ ] Delete it → verify it disappears
- [ ] Stop app with Ctrl+C

### Step 2.5 — Commit

- [ ] Run:
```bash
git add app.py
git commit -m "feat: add Scheduled Emails page for leaders"
```

---

## Task 3: Create scheduler.py standalone process

**Files:**
- Create: `scheduler.py`

### Step 3.1 — Create scheduler.py

- [ ] Create `/Users/vishaljangid/learning/harshit/mcp/task_tracker/scheduler.py`:

```python
"""
scheduler.py — Standalone scheduled email sender.

Run with: python scheduler.py
Polls every 60 seconds. When a schedule's send_time matches current HH:MM
and the current day matches the schedule's days setting, sends the email.
"""

import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from database import (
    get_all_active_schedules,
    get_all_teams_updates_by_date,
    get_meeting_notes,
    get_missing_users_today,
    get_team_by_id,
    get_team_members_emails,
    get_team_updates_by_date_range,
    get_users_by_team,
)
from email_utils import send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [scheduler] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Tracks (schedule_id, date_str, hhmm) already sent this process run
_fired: set = set()


def _strip_html(h: str) -> str:
    if not h:
        return ""
    h = re.sub(r"</p>|<p[^>]*>", "\n", h)
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"<li[^>]*>", "\n• ", h)
    h = re.sub(r"<[^>]+>", "", h)
    import html as _html
    h = _html.unescape(h).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def _format_date(date_str: str) -> str:
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d").strftime("%-d %B %Y")
    except Exception:
        return str(date_str)


def _format_name(name: str) -> str:
    return " ".join(w.capitalize() for w in (name or "").split())


def _build_schedule_email_body(
    team_id: int,
    team_name: str,
    target: str,
    inc_updates: bool,
    inc_mom: bool,
) -> str:
    sections = []

    if inc_updates:
        rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team_id]
        missing = get_missing_users_today(team_id, target)

        update_cards = ""
        if rows:
            for r in rows:
                plain = _strip_html(r["content"]).replace("\n", "<br>")
                update_cards += f"""
                <div style="margin-bottom:14px;padding:14px 16px;background:#f8f9fa;
                            border-left:4px solid #4f46e5;border-radius:6px;">
                  <div style="font-weight:700;color:#1f2937;font-size:14px;margin-bottom:6px;">
                    {_format_name(r['user_name'])}
                  </div>
                  <div style="color:#374151;font-size:13px;line-height:1.7;">{plain}</div>
                </div>"""
        else:
            update_cards = '<p style="color:#6b7280;font-style:italic;">No updates submitted.</p>'

        missing_html = ""
        if missing:
            names_li = "".join(
                f'<li style="color:#92400e;margin-bottom:4px;">'
                f'{_format_name(r["user_name"])} &lt;{r["email"]}&gt;</li>'
                for r in missing
            )
            missing_html = f"""
            <div style="margin-top:16px;padding:14px 16px;background:#fffbeb;
                        border:1px solid #fcd34d;border-radius:6px;">
              <div style="font-weight:700;color:#92400e;margin-bottom:8px;">
                ⚠ Missing Submissions ({len(missing)})
              </div>
              <ul style="margin:0;padding-left:20px;">{names_li}</ul>
            </div>"""

        sections.append(f"""
        <div style="margin-bottom:28px;">
          <h2 style="font-size:16px;font-weight:700;color:#4f46e5;
                     border-bottom:2px solid #e5e7eb;padding-bottom:6px;margin-bottom:12px;">
            📋&nbsp; Daily Updates
          </h2>
          {update_cards}{missing_html}
        </div>""")

    if inc_mom:
        notes = get_meeting_notes(team_id, target)
        if notes:
            mom_html = f'<div style="color:#374151;font-size:14px;line-height:1.7;">{_strip_html(notes["content"]).replace(chr(10), "<br>")}</div>'
        else:
            mom_html = '<p style="color:#6b7280;font-style:italic;">No meeting notes recorded.</p>'

        sections.append(f"""
        <div style="margin-bottom:28px;">
          <h2 style="font-size:16px;font-weight:700;color:#4f46e5;
                     border-bottom:2px solid #e5e7eb;padding-bottom:6px;margin-bottom:16px;">
            📝&nbsp; Minutes of Meeting
          </h2>
          {mom_html}
        </div>""")

    intro = (
        "daily updates and meeting notes" if inc_updates and inc_mom
        else "daily updates" if inc_updates
        else "meeting notes"
    )
    body_html = "".join(sections)
    return f"""<html-body>
    <p style="color:#374151;margin-bottom:24px;">
      Hi Team,<br><br>
      Please find the <strong>{intro}</strong> for
      <strong>{_format_name(team_name)}</strong> — {_format_date(target)} below.
    </p>
    {body_html}
    </html-body>"""


def _should_fire_today(days_setting: str) -> bool:
    weekday = datetime.now().weekday()  # 0=Mon, 6=Sun
    if days_setting == "weekdays":
        return weekday < 5
    return True  # "daily"


def _fire_schedule(schedule) -> None:
    team_id = schedule["team_id"]
    label = schedule["label"]
    content_type = schedule["content_type"]
    auto_cc_team = bool(schedule["auto_cc_team"])

    team = get_team_by_id(team_id)
    if not team:
        log.warning("Schedule '%s': team_id=%d not found, skipping.", label, team_id)
        return

    target = date.today().isoformat()
    inc_updates = content_type in ("updates", "both")
    inc_mom = content_type in ("mom", "both")

    try:
        body = _build_schedule_email_body(team_id, team["name"], target, inc_updates, inc_mom)
    except Exception as e:
        log.error("Schedule '%s': failed to build email body: %s", label, e)
        return

    kind = {"updates": "Daily Updates", "mom": "Meeting Notes", "both": "Daily Updates & MoM"}.get(
        content_type, "Report"
    )
    subject = f"{kind} — {_format_name(team['name'])} — {_format_date(target)}"

    manual_recipients = json.loads(schedule["recipients"] or "[]")
    if not manual_recipients and not auto_cc_team:
        log.warning("Schedule '%s': no recipients configured, skipping.", label)
        return

    team_emails = get_team_members_emails(team_id) if auto_cc_team else []

    # First manual recipient is To:, rest are CC along with team emails
    if manual_recipients:
        to_email = manual_recipients[0]
        cc = list(dict.fromkeys(
            manual_recipients[1:] + [e for e in team_emails if e.lower() != to_email.lower()]
        ))
    else:
        to_email = team_emails[0] if team_emails else None
        cc = list(dict.fromkeys(team_emails[1:])) if len(team_emails) > 1 else []

    if not to_email:
        log.warning("Schedule '%s': could not determine To: address, skipping.", label)
        return

    ok, msg = send_email(to_email, subject, body, cc)
    if ok:
        log.info("Schedule '%s': email sent to %s (CC: %d).", label, to_email, len(cc))
    else:
        log.error("Schedule '%s': failed to send to %s: %s", label, to_email, msg)


def run_once() -> None:
    now_hhmm = datetime.now().strftime("%H:%M")
    today_str = date.today().isoformat()

    schedules = get_all_active_schedules()
    for s in schedules:
        if s["send_time"] != now_hhmm:
            continue
        if not _should_fire_today(s["days"]):
            continue
        fire_key = (s["id"], today_str, now_hhmm)
        if fire_key in _fired:
            continue
        _fired.add(fire_key)
        log.info("Firing schedule '%s' (id=%d).", s["label"], s["id"])
        _fire_schedule(s)


def main() -> None:
    log.info("Scheduler started. Checking every 60 seconds.")
    while True:
        try:
            run_once()
        except Exception as e:
            log.error("Unexpected error in run_once: %s", e)
        time.sleep(60)


if __name__ == "__main__":
    main()
```

### Step 3.2 — Smoke-test scheduler logic manually

- [ ] Run a quick one-off check (no real email needed — just verify DB reads work):

```bash
cd /Users/vishaljangid/learning/harshit/mcp/task_tracker && python -c "
from scheduler import run_once, _fired
print('Scheduler imports OK')
run_once()
print('run_once() completed without crash')
"
```

Expected: `Scheduler imports OK` and `run_once() completed without crash` (no fired schedules unless current time matches a saved schedule).

### Step 3.3 — End-to-end test with a schedule set to fire now

- [ ] Open Streamlit app, log in as Alice, go to "Scheduled Emails"
- [ ] Add a schedule:
  - Label: `Test Now`
  - Time: set to current time + 1 minute (e.g. if it's 14:23, set 14:24)
  - Daily, Both, auto-CC off, recipient: your email
- [ ] In a second terminal, run: `python scheduler.py`
- [ ] Wait for the minute to tick over
- [ ] Verify: console shows `Firing schedule 'Test Now'` and email arrives in inbox
- [ ] Stop scheduler with Ctrl+C

### Step 3.4 — Commit

- [ ] Run:
```bash
git add scheduler.py
git commit -m "feat: add standalone email scheduler process"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered in |
|---|---|
| New `email_schedules` DB table | Task 1, Step 1.4 |
| 5 CRUD functions | Task 1, Step 1.5 |
| Leader-only "Scheduled Emails" page | Task 2 |
| Add/toggle/delete schedules in UI | Task 2, Step 2.2 |
| Sidebar entry for leaders | Task 2, Step 2.3 |
| Daily or weekdays schedule | Task 3, `_should_fire_today()` |
| Content type: updates / mom / both | Task 3, `_build_schedule_email_body()` |
| Auto-CC team members option | Task 3, `_fire_schedule()` |
| Manual recipient emails | Task 3, `_fire_schedule()` |
| Already-sent guard | Task 3, `_fired` set |
| Separate scheduler process | Task 3 |

**Placeholder scan:** No TBDs or vague steps found.

**Type consistency check:**
- `create_email_schedule(... recipients_json: str ...)` — used as `json.dumps(list)` in app.py ✓
- `get_all_active_schedules()` returns sqlite3.Row objects — accessed by key in scheduler.py ✓
- `toggle_schedule(schedule_id, is_active: bool)` — called with `bool` in app.py ✓
- `delete_schedule(schedule_id)` — called with `s["id"]` in app.py ✓
