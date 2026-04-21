"""
database.py – SQLite persistence layer for Team Daily Update Tracker.

Each public function opens its own connection (via _get_conn()), performs
its work, and closes the connection in a finally block.
"""

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEFAULT_DB = Path(__file__).parent / "tracker.db"


def _get_conn() -> sqlite3.Connection:
    db_path = Path(os.environ.get("TRACKER_DB_OVERRIDE") or _DEFAULT_DB)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS teams (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('member','leader','manager')),
    team_id       INTEGER REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS daily_updates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    content    TEXT NOT NULL,
    date       TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_date ON daily_updates(user_id, date);

CREATE TABLE IF NOT EXISTS meeting_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id    INTEGER NOT NULL REFERENCES teams(id),
    date       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_team_date_mom ON meeting_notes(team_id, date);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

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
    last_sent_at  TEXT DEFAULT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_send_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id      INTEGER REFERENCES email_schedules(id) ON DELETE SET NULL,
    team_id          INTEGER NOT NULL,
    label            TEXT NOT NULL,
    sent_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status           TEXT NOT NULL CHECK(status IN ('success','failed')),
    recipients_count INTEGER DEFAULT 0,
    error_message    TEXT DEFAULT NULL
);
"""


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables and seed demo data if teams table is empty."""
    try:
        from task_tracker.auth import hash_password
    except ModuleNotFoundError:
        from auth import hash_password

    conn = _get_conn()
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

        # Clean up expired sessions on startup
        conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
        conn.commit()

        # Migrate: add last_sent_at column if missing (existing databases)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(email_schedules)").fetchall()]
        if "last_sent_at" not in cols:
            conn.execute("ALTER TABLE email_schedules ADD COLUMN last_sent_at TEXT DEFAULT NULL")
            conn.commit()

        # Migrate: create email_send_log table if missing (existing databases)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS email_send_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id      INTEGER REFERENCES email_schedules(id) ON DELETE SET NULL,
                team_id          INTEGER NOT NULL,
                label            TEXT NOT NULL,
                sent_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status           TEXT NOT NULL CHECK(status IN ('success','failed')),
                recipients_count INTEGER DEFAULT 0,
                error_message    TEXT DEFAULT NULL
            );
        """)
        conn.commit()

        # Seed history from existing last_sent_at records (runs every startup, skips duplicates)
        sent_schedules = conn.execute(
            "SELECT id, team_id, label, last_sent_at FROM email_schedules WHERE last_sent_at IS NOT NULL"
        ).fetchall()
        for s in sent_schedules:
            already = conn.execute(
                "SELECT 1 FROM email_send_log WHERE schedule_id=? AND sent_at=?",
                (s[0], s[3]),
            ).fetchone()
            if not already:
                conn.execute(
                    """INSERT INTO email_send_log
                       (schedule_id, team_id, label, sent_at, status, recipients_count)
                       VALUES (?,?,?,?,?,?)""",
                    (s[0], s[1], s[2], s[3], "success", 0),
                )
        conn.commit()

        # Only seed if no teams exist yet
        row = conn.execute("SELECT COUNT(*) AS cnt FROM teams").fetchone()
        if row["cnt"] > 0:
            return

        # ---- Teams --------------------------------------------------------
        conn.execute("INSERT INTO teams (id, name) VALUES (1, 'Alpha')")
        conn.execute("INSERT INTO teams (id, name) VALUES (2, 'Beta')")
        conn.execute("INSERT INTO teams (id, name) VALUES (3, 'Gamma')")

        # ---- Users --------------------------------------------------------
        pw = hash_password("password123")

        users = [
            ("Alice Leader",  "alice@example.com",  "leader",  1),
            ("Bob Member",    "bob@example.com",    "member",  1),
            ("Carol Member",  "carol@example.com",  "member",  1),
            ("Dave Leader",   "dave@example.com",   "leader",  2),
            ("Eve Member",    "eve@example.com",    "member",  2),
            ("Frank Manager", "frank@example.com",  "manager", None),
        ]

        for name, email, role, team_id in users:
            conn.execute(
                "INSERT INTO users (name, email, password_hash, role, team_id) VALUES (?,?,?,?,?)",
                (name, email, pw, role, team_id),
            )

        conn.commit()

        def uid(email: str) -> int:
            return conn.execute(
                "SELECT id FROM users WHERE email=?", (email,)
            ).fetchone()["id"]

        alice_id = uid("alice@example.com")
        bob_id   = uid("bob@example.com")
        carol_id = uid("carol@example.com")
        eve_id   = uid("eve@example.com")

        today     = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        updates = [
            (
                bob_id, yesterday,
                "<p><strong>Yesterday:</strong></p><ul><li>Implemented login module</li>"
                "<li>Fixed auth bug #101</li></ul><p><strong>Today:</strong></p><ul>"
                "<li>Working on user profile page</li><li>Code review for PR #45</li></ul>"
                "<p><strong>Blockers:</strong> None</p>",
            ),
            (
                carol_id, yesterday,
                "<p><strong>Yesterday:</strong></p><ul><li>Designed landing page mockup</li>"
                "<li>Updated color scheme per feedback</li></ul><p><strong>Today:</strong></p>"
                "<ul><li>Implement landing page</li><li>Mobile layout</li></ul>"
                "<p><strong>Blockers:</strong> Waiting for client assets</p>",
            ),
            (
                alice_id, yesterday,
                "<p><strong>Yesterday:</strong></p><ul><li>Team planning session</li>"
                "<li>Reviewed sprint backlog</li></ul><p><strong>Today:</strong></p><ul>"
                "<li>1:1s with team members</li><li>Architecture review</li></ul>"
                "<p><strong>Blockers:</strong> None</p>",
            ),
            (
                bob_id, today,
                "<p><strong>Yesterday:</strong></p><ul><li>Completed user profile page</li>"
                "<li>Merged PR #45</li></ul><p><strong>Today:</strong></p><ul>"
                "<li>Starting dashboard widget</li><li>Team standup</li></ul>"
                "<p><strong>Blockers:</strong> None</p>",
            ),
            (
                alice_id, today,
                "<p><strong>Yesterday:</strong></p><ul><li>Architecture review done</li>"
                "<li>Unblocked backend team</li></ul><p><strong>Today:</strong></p><ul>"
                "<li>Sprint retrospective</li><li>Client call at 3pm</li></ul>"
                "<p><strong>Blockers:</strong> None</p>",
            ),
            (
                eve_id, today,
                "<p><strong>Yesterday:</strong></p><ul><li>Setup dev environment</li></ul>"
                "<p><strong>Today:</strong></p><ul><li>Starting on API integration</li></ul>"
                "<p><strong>Blockers:</strong> None</p>",
            ),
        ]

        for user_id, upd_date, content in updates:
            conn.execute(
                "INSERT INTO daily_updates (user_id, content, date) VALUES (?,?,?)",
                (user_id, content, upd_date),
            )

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def get_all_teams() -> list:
    conn = _get_conn()
    try:
        return conn.execute("SELECT id, name FROM teams ORDER BY name").fetchall()
    finally:
        conn.close()


def get_team_by_id(team_id: int):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT id, name FROM teams WHERE id=?", (team_id,)
        ).fetchone()
    finally:
        conn.close()


def create_team(name: str) -> int:
    conn = _get_conn()
    try:
        cursor = conn.execute("INSERT INTO teams (name) VALUES (?)", (name.strip(),))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_team_name(team_id: int, name: str) -> None:
    conn = _get_conn()
    try:
        conn.execute("UPDATE teams SET name=? WHERE id=?", (name.strip(), team_id))
        conn.commit()
    finally:
        conn.close()


def get_team_leader(team_id: int):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT id, name, email FROM users WHERE team_id=? AND role='leader'",
            (team_id,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(
    name: str,
    email: str,
    password_hash: str,
    role: str,
    team_id: int | None,
) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role, team_id) VALUES (?,?,?,?,?)",
            (name, email, password_hash, role, team_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_user_by_email(email: str):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()


def get_user_by_name(name: str):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE name LIKE ? COLLATE NOCASE",
            (name,),
        ).fetchone()
    finally:
        conn.close()


def get_users_by_team(team_id: int) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE team_id=?", (team_id,)
        ).fetchall()
    finally:
        conn.close()


def get_all_users_with_teams() -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            """
            SELECT u.*, t.name AS team_name
            FROM users u
            LEFT JOIN teams t ON u.team_id = t.id
            ORDER BY u.name
            """
        ).fetchall()
    finally:
        conn.close()


def get_leaders() -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE role='leader'"
        ).fetchall()
    finally:
        conn.close()


def get_managers() -> list:
    """Return all users with role='manager' (org-wide, not tied to a team)."""
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE role='manager'"
        ).fetchall()
    finally:
        conn.close()


def get_team_members_emails(team_id: int) -> list[str]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT email FROM users WHERE team_id=?", (team_id,)
        ).fetchall()
        return [row["email"] for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daily Updates
# ---------------------------------------------------------------------------

def create_update(user_id: int, content: str, date: str) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO daily_updates (user_id, content, date) VALUES (?,?,?)",
            (user_id, content, date),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def edit_update(update_id: int, content: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            UPDATE daily_updates
            SET content=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (content, update_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_updates_by_user(user_id: int) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM daily_updates WHERE user_id=? ORDER BY date DESC, created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def get_updates_by_user_and_days(user_id: int, days: int) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            """
            SELECT * FROM daily_updates
            WHERE user_id=? AND date >= date('now', ? || ' days')
            ORDER BY date DESC, created_at DESC
            """,
            (user_id, f"-{days}"),
        ).fetchall()
    finally:
        conn.close()


def get_update_today(user_id: int, date: str):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM daily_updates WHERE user_id=? AND date=?",
            (user_id, date),
        ).fetchone()
    finally:
        conn.close()


def get_team_updates_by_date(team_id: int, date: str) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            """
            SELECT
                u.name        AS user_name,
                u.id          AS user_id,
                d.id          AS update_id,
                d.content,
                d.created_at,
                d.updated_at
            FROM users u
            JOIN daily_updates d ON d.user_id = u.id
            WHERE u.team_id=? AND d.date=?
            ORDER BY u.name
            """,
            (team_id, date),
        ).fetchall()
    finally:
        conn.close()


def get_missing_users_today(team_id: int, date: str) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            """
            SELECT
                u.name  AS user_name,
                u.email AS email
            FROM users u
            LEFT JOIN daily_updates d
                ON d.user_id = u.id AND d.date = ?
            WHERE u.team_id = ? AND d.id IS NULL
            ORDER BY u.name
            """,
            (date, team_id),
        ).fetchall()
    finally:
        conn.close()


def get_team_updates_by_date_range(team_id: int, from_date: str, to_date: str) -> list:
    """Fetch a team's updates between from_date and to_date (inclusive), newest first."""
    conn = _get_conn()
    try:
        return conn.execute(
            """
            SELECT
                u.id          AS user_id,
                u.name        AS user_name,
                u.role        AS user_role,
                d.id          AS update_id,
                d.content,
                d.date,
                d.created_at,
                d.updated_at
            FROM users u
            JOIN daily_updates d ON d.user_id = u.id
            WHERE u.team_id = ? AND d.date BETWEEN ? AND ?
            ORDER BY d.date DESC, u.name
            """,
            (team_id, from_date, to_date),
        ).fetchall()
    finally:
        conn.close()


def get_all_teams_updates_by_date(date: str) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            """
            SELECT
                t.id          AS team_id,
                t.name        AS team_name,
                u.id          AS user_id,
                u.name        AS user_name,
                u.role        AS user_role,
                d.content,
                d.created_at,
                d.updated_at
            FROM teams t
            JOIN users u ON u.team_id = t.id
            JOIN daily_updates d ON d.user_id = u.id
            WHERE d.date = ?
            ORDER BY t.name, u.name
            """,
            (date,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Meeting Notes
# ---------------------------------------------------------------------------

def upsert_meeting_notes(
    team_id: int,
    date: str,
    content: str,
    created_by: int,
) -> None:
    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM meeting_notes WHERE team_id=? AND date=?",
            (team_id, date),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO meeting_notes (team_id, date, content, created_by)
                VALUES (?,?,?,?)
                """,
                (team_id, date, content, created_by),
            )
        else:
            conn.execute(
                """
                UPDATE meeting_notes
                SET content=?, updated_at=CURRENT_TIMESTAMP
                WHERE team_id=? AND date=?
                """,
                (content, team_id, date),
            )

        conn.commit()
    finally:
        conn.close()


def get_meeting_notes(team_id: int, date: str):
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM meeting_notes WHERE team_id=? AND date=?",
            (team_id, date),
        ).fetchone()
    finally:
        conn.close()


def get_all_meeting_notes(team_id: int) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT * FROM meeting_notes WHERE team_id=? ORDER BY date DESC",
            (team_id,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(token: str, user_id: int, days: int = 7) -> None:
    expires_at = (datetime.now() + timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_session_user(token: str):
    conn = _get_conn()
    try:
        return conn.execute(
            """SELECT u.id, u.name, u.email, u.role, u.team_id
               FROM sessions s
               JOIN users u ON s.user_id = u.id
               WHERE s.token = ? AND s.expires_at > datetime('now')""",
            (token,),
        ).fetchone()
    finally:
        conn.close()


def delete_session(token: str) -> None:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


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


def log_email_send(
    schedule_id: int,
    team_id: int,
    label: str,
    status: str,
    recipients_count: int = 0,
    error_message: str = None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO email_send_log
               (schedule_id, team_id, label, status, recipients_count, error_message)
               VALUES (?,?,?,?,?,?)""",
            (schedule_id, team_id, label, status, recipients_count, error_message),
        )
        conn.commit()
    finally:
        conn.close()


def get_team_email_history(team_id: int, limit: int = 100) -> list:
    conn = _get_conn()
    try:
        return conn.execute(
            """SELECT * FROM email_send_log
               WHERE team_id = ?
               ORDER BY sent_at DESC
               LIMIT ?""",
            (team_id, limit),
        ).fetchall()
    finally:
        conn.close()


def try_claim_schedule_send(schedule_id: int, sent_key: str) -> bool:
    """Atomically mark a schedule as sent for sent_key ("YYYY-MM-DD HH:MM").
    Returns True if this caller claimed it (should send), False if already claimed."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE email_schedules
               SET last_sent_at = ?
               WHERE id = ? AND (last_sent_at IS NULL OR last_sent_at != ?)""",
            (sent_key, schedule_id, sent_key),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()