"""
database.py – SQLite persistence layer for Team Daily Update Tracker.

Each public function opens its own connection (via _get_conn()), performs
its work, and closes the connection in a finally block.
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "tracker.db"  # absolute, works from any cwd


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
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