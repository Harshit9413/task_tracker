# Auto-Scheduled Email Feature — Design Spec
Date: 2026-04-20

## Overview

Team leaders can create multiple scheduled email jobs. At the configured time, the system automatically sends team updates and/or meeting notes to a specified list of recipients. The scheduler runs as a separate process independent of the Streamlit app.

## Requirements

- Leader can create multiple schedules per team
- Each schedule has: label, send time (HH:MM), days (daily / weekdays), recipients, content type
- Content type: daily updates, meeting notes (MoM), or both
- Recipients: manually typed emails + optional auto-CC of all team members
- Leader can activate/deactivate or delete any schedule
- Emails fire automatically at the scheduled time without any manual action

## Architecture

### Two-process model

```
[ Streamlit App (app.py) ]       [ Scheduler Process (scheduler.py) ]
        |                                      |
        | reads/writes                         | reads
        v                                      v
   [ SQLite DB — email_schedules table ]
```

Streamlit manages schedule configuration. The scheduler process runs independently, polling every 60 seconds.

## Database Schema

New table added to `database.py`:

```sql
CREATE TABLE IF NOT EXISTS email_schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id       INTEGER NOT NULL REFERENCES teams(id),
    created_by    INTEGER NOT NULL REFERENCES users(id),
    label         TEXT NOT NULL,
    send_time     TEXT NOT NULL,      -- "HH:MM" 24-hour format
    days          TEXT NOT NULL,      -- "daily" or "weekdays"
    recipients    TEXT NOT NULL,      -- JSON array of email strings
    auto_cc_team  INTEGER DEFAULT 1,  -- 1 = auto-CC team members
    content_type  TEXT DEFAULT 'both',-- 'updates', 'mom', or 'both'
    is_active     INTEGER DEFAULT 1,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### New database.py functions

- `create_email_schedule(team_id, created_by, label, send_time, days, recipients, auto_cc_team, content_type)` → int
- `get_team_schedules(team_id)` → list
- `get_all_active_schedules()` → list (used by scheduler process)
- `toggle_schedule(schedule_id, is_active)` → None
- `delete_schedule(schedule_id)` → None

## Streamlit UI

New sidebar page: **"Scheduled Emails"** (visible to leaders only).

### Page layout

1. **Active schedules list** — cards showing label, time, days, recipients, content type
   - Toggle (active/inactive) per schedule
   - Delete button per schedule

2. **"Add New Schedule" expander/form:**
   - Label text input
   - Time input (HH:MM) — default "18:00"
   - Days radio: "Daily" / "Weekdays (Mon–Fri)"
   - Recipients textarea (comma-separated emails)
   - Checkbox: "Auto-CC all team members"
   - Content radio: "Updates only" / "Meeting Notes only" / "Both"
   - Submit button

### Routing

In `app.py` `main()`:
- Add "Scheduled Emails" to leader pages list
- Add `elif page == "Scheduled Emails": show_scheduled_emails()` routing

## Scheduler Process (scheduler.py)

Standalone script using APScheduler. Run with: `python scheduler.py`

### Logic (runs every 60 seconds)

```
1. Get current HH:MM and current weekday
2. Fetch all active schedules from DB
3. For each schedule:
   a. Check if send_time matches current time
   b. Check if today's day matches schedule's days setting
   c. If both match:
      - Build recipient list (manual emails + team members if auto_cc_team)
      - Build email body using chatbot._build_email_body()
      - Call send_email() for first recipient, CC rest
      - Log result
4. Sleep until next minute
```

### Already-sent guard

Track `(schedule_id, date, HH:MM)` in a local set per process run to prevent double-firing within same minute window.

### Dependencies

- `apscheduler` (add to requirements if not present) — OR use simple `time.sleep(60)` loop (no extra dependency)
- All existing project modules: `database`, `email_utils`, `chatbot._build_email_body`

### Recommendation

Use simple `while True: time.sleep(60)` loop — no extra dependency needed, equally reliable for this use case.

## File Changes Summary

| File | Change |
|------|--------|
| `database.py` | Add `email_schedules` table to schema + 5 CRUD functions |
| `app.py` | Add `show_scheduled_emails()` page + sidebar entry for leaders |
| `scheduler.py` | New file — standalone scheduler process |
| `chatbot.py` | No changes |
| `api.py` | No changes |

## Error Handling

- If email fails, log to console with schedule label and error message
- If team has no updates for the day, email is still sent (body shows "No updates submitted")
- Invalid time format in DB is skipped with a warning log

## Out of Scope

- Email delivery confirmation / read receipts
- Per-schedule timezone support (uses server local time)
- Web-based scheduler dashboard / logs UI
