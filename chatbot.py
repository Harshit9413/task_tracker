import re
import html as html_module
from datetime import date as dt_date, datetime as dt_datetime, timedelta
from functools import lru_cache
from typing import Union, Optional
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from email_utils import send_email
from database import (
    get_user_by_name, get_updates_by_user_and_days, get_all_teams_updates_by_date,
    get_team_updates_by_date_range,
    get_missing_users_today, get_all_teams, get_users_by_team,
    get_team_members_emails, get_meeting_notes as db_get_meeting_notes,
    get_managers,
)

_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH, override=True)

_current_user   = None
_last_context   = None
_last_recipient = None
_last_user_sent = None


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _row_get(row, key, default=None):
    if row is None: return default
    try:
        v = row[key]; return default if v is None else v
    except (KeyError, IndexError): return default

def _is_leader(user) -> bool:
    if not user: return False
    return (_row_get(user, "role") or "").strip().lower() in (
        "leader", "lead", "team_leader", "team-leader", "manager", "admin")

def _check_leader() -> Optional[str]:
    if not _current_user: return "Access denied: user information not available."
    if not _is_leader(_current_user): return "Access denied: only team leaders can view or send team data."
    return None

def _own_team_id():   return _row_get(_current_user, "team_id")
def _own_team_name(): return _row_get(_current_user, "team_name")

def _own_team():
    tid = _own_team_id()
    for t in get_all_teams():
        if tid is not None and t["id"] == tid: return t
        if t["name"].lower() == (_own_team_name() or "").lower(): return t
    return None

def _user_in_own_team(name: str) -> bool:
    t = _own_team()
    return bool(t and any(
        m["name"].lower() == name.lower() for m in get_users_by_team(t["id"])
    ))

def _has(text, pats): return any(p in text for p in pats)

def _has_ref(text):
    REF = [" this ", " this.", " that ", " it ", " it.",
           " same ", " above ", " yeh ", " wahi ", " upar "]
    return any(w in f" {text.strip()} " for w in REF)

_TODAY_WORDS = ["today", "aaj", "abhi", "aaj ka", "aaj ki"]
_PREV_WORDS  = [
    "yesterday", "previous", "prev", "before today",
    "kal", "purana", "purani", "purane",
    "pichla", "pichli", "pichle din", "pichli baar",
    "pehle ka", "pehle ki",
]


def _has_today(text: str) -> bool:
    t = f" {text.strip()} "
    return any(f" {w} " in t for w in _TODAY_WORDS)


def _has_prev(text: str) -> bool:
    t = f" {text.strip()} "
    return any(f" {w} " in t for w in _PREV_WORDS)


def _detect_days(text: str) -> int:
    # Explicit "N days / N din" always wins
    m = re.search(r"(\d+)\s*(day|din)", text)
    if m: return max(int(m.group(1)), 1)
    if "week" in text or "hafte" in text or "hafta" in text: return 7
    if "last month" in text or "pichle mahine" in text: return 30
    # "today/aaj" overrides everything → 1
    if _has_today(text): return 1
    # explicit previous keyword → 2
    if _has_prev(text): return 2
    return 1


def _wants_previous_only(text: str) -> bool:
    return _has_prev(text) and not _has_today(text)


_YESTERDAY_WORDS = [
    "yesterday", "kal", "kal ki", "kal ka",
    "purana", "purani", "purane",
    "pichla din", "pichle din", "previous day",
]

def _is_yesterday_only(text: str) -> bool:
    """Return True when user means exactly yesterday (not a multi-day range)."""
    # if there's an explicit number like "3 days/din", it's a range not yesterday
    if re.search(r"\d+\s*(day|din)", text):
        return False
    if "week" in text or "hafte" in text:
        return False
    t = f" {text.strip()} "
    return any(f" {w} " in t for w in _YESTERDAY_WORDS)


def _yesterday_date() -> str:
    return (dt_date.today() - timedelta(days=1)).isoformat()


_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _extract_date(text: str) -> Optional[str]:
    """Try to parse a specific date from user text.
    Handles: '15 april 2026', '15 april', 'april 15', '15/04/2026', '2026-04-15'
    Returns ISO date string (YYYY-MM-DD) or None.
    """
    tl = text.lower()
    cur_year = dt_date.today().year

    # ISO / numeric: 2026-04-15 or 15/04/2026 or 15-04-2026
    m = re.search(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b", tl)
    if m:
        try:
            return dt_date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", tl)
    if m:
        try:
            return dt_date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass

    # "15 april 2026" or "15 april"
    m = re.search(r"\b(\d{1,2})\s+([a-z]+)\s*(\d{4})?\b", tl)
    if m:
        day, mon_str, yr = int(m.group(1)), m.group(2), m.group(3)
        month = _MONTHS.get(mon_str)
        if month:
            year = int(yr) if yr else cur_year
            try:
                return dt_date(year, month, day).isoformat()
            except ValueError:
                pass

    # "april 15 2026" or "april 15"
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})?\b", tl)
    if m:
        mon_str, day, yr = m.group(1), int(m.group(2)), m.group(3)
        month = _MONTHS.get(mon_str)
        if month:
            year = int(yr) if yr else cur_year
            try:
                return dt_date(year, month, day).isoformat()
            except ValueError:
                pass

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# HTML STRIPPER  +  CHATGPT-STYLE FORMATTER
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_html(h: str) -> str:
    """Strip HTML — handles both inline and multiline HTML from rich text editors."""
    if not h: return ""
    h = re.sub(r'</p>',       '\n', h)
    h = re.sub(r'<p[^>]*>',   '\n', h)
    h = re.sub(r'</ul>',      '\n', h)
    h = re.sub(r'<ul[^>]*>',  '\n', h)
    h = re.sub(r'</ol>',      '\n', h)
    h = re.sub(r'<ol[^>]*>',  '\n', h)
    h = re.sub(r'<br\s*/?>', '\n', h)
    h = re.sub(r'<li[^>]*>',  '\n• ', h)
    h = re.sub(r'</li>',       '', h)
    h = re.sub(r'<[^>]+>',    '', h)
    h = html_module.unescape(h).replace('\xa0', ' ')
    return re.sub(r'\n{3,}', '\n', h).strip()


def _format_date(date_str: str) -> str:
    """2026-04-16 → 16 April 2026"""
    try:
        return dt_datetime.strptime(str(date_str), "%Y-%m-%d").strftime("%-d %B %Y")
    except Exception:
        return str(date_str)


def _format_name(name: str) -> str:
    """harish kumar → Harish Kumar"""
    return " ".join(w.capitalize() for w in (name or "").split())


_SECTION_MAP = [
    (["tasks completed", "completed tasks", "work completed",
      "kaam kiya", "done today", "finished"],             "🔹 Tasks Completed"),
    (["work in progress", "wip", "in progress",
      "ongoing", "chal raha", "currently working"],       "🔄 Work in Progress"),
    (["issues", "blockers", "problems", "issue",
      "dikkat", "problem", "blocker"],                    "⚠️  Issues"),
    (["pending", "remaining", "baaki", "left"],           "⏳ Pending"),
    (["tomorrow", "plan for tomorrow", "next steps",
      "kal ka plan", "upcoming", "plan"],                  "📌 Plan for Tomorrow"),
    (["meeting", "meetings", "calls", "discussion"],      "📞 Meetings"),
    (["notes", "other", "misc", "additional",
      "remarks", "extra"],                                "📝 Notes"),
    (["achievements", "highlights", "wins",
      "accomplishments"],                                  "⭐ Highlights"),
]


def _match_section(line: str) -> Optional[str]:
    """Return emoji label if line is a section header, else None."""
    # strip leading emojis, bullets, spaces, asterisks, dashes
    clean = re.sub(r'^[^\w\u0900-\u097F]+', '', line).strip()
    # strip bold markdown asterisks
    clean = re.sub(r'\*+', '', clean).strip()
    # lowercase + strip trailing colon/dash
    clean = clean.lower().rstrip(":").rstrip("-").strip()
    for keywords, label in _SECTION_MAP:
        for kw in keywords:
            if clean == kw or clean.startswith(kw):
                return label
    return None


def _format_update(user_name: str, date: str, raw_content: str) -> str:
    """
    📅  Date: 16 April 2026
    👤  Employee: Harish Kumar

    🔹 Tasks Completed:
      •  Resolved 8 technical support tickets

    🔄 Work in Progress:
      •  Monitoring server performance

    ⚠️  Issues:
      •  Slow internet speed in accounts department
    """
    plain     = _strip_html(raw_content)
    sections  = []
    cur_label = None
    cur_items = []

    for line in [l.strip() for l in plain.splitlines()]:
        if not line: continue
        label = _match_section(line)
        if label:
            if cur_label and cur_items:
                sections.append((cur_label, cur_items))
            elif cur_items:
                sections.append(("🔹 Tasks Completed", cur_items))
            cur_label = label
            cur_items = []
        else:
            item = re.sub(r'^[•\-\*]\s*', '', line).strip()
            if item:
                cur_items.append(item)

    if cur_label and cur_items:
        sections.append((cur_label, cur_items))
    elif cur_items:
        sections.append(("🔹 Tasks Completed", cur_items))

    out = [
        f"👤  {_format_name(user_name)}  —  📅  {_format_date(date)}",
        "",
    ]
    if not sections:
        out.append(plain)
        return "\n".join(out)

    for label, items in sections:
        out.append(label)
        out.append("")
        for item in items:
            out.append(f"• {item}")
        out.append("")

    return "\n".join(out).rstrip()


def _format_team_block(team_name: str, target: str, rows: list) -> str:
    divider = "─" * 44
    parts = [
        f"📋  Team Updates — {team_name}",
        f"📅  {_format_date(target)}",
        divider,
    ]
    for r in rows:
        parts.append("")
        parts.append(_format_update(r["user_name"], target, r["content"]))
        parts.append(divider)
    return "\n".join(parts).strip()


# ═══════════════════════════════════════════════════════════════════════════════
# NAME / EMAIL LOOKUP
# ═══════════════════════════════════════════════════════════════════════════════

def _all_candidates():
    t = _own_team()
    c = list(get_users_by_team(t["id"])) if t else []
    c.extend(get_managers())
    return c

def _find_member_in_text(text: str, members: list):
    tl = text.lower()
    for m in members:
        full = (m["name"] or "").lower().strip()
        if full and full in tl: return m
    for m in members:
        first = (m["name"] or "").split()[0].lower() if m["name"] else ""
        if first and re.search(rf"\b{re.escape(first)}\b", tl): return m
    return None

def _email_from_text(text: str) -> Optional[str]:
    m = _find_member_in_text(text, _all_candidates())
    return m["email"] if m else None

def _email_after_keyword(text: str) -> Optional[str]:
    tl = text.lower()
    for sep in [" to ", " ko ", " taraf ", " ke liye "]:
        pos = tl.rfind(sep)
        if pos != -1:
            e = _email_from_text(text[pos + len(sep):])
            if e: return e
    return None

def _parse_send_intent(text: str, members: list):
    tl = text.lower()
    nmap = {}
    for m in members:
        if not m["name"]: continue
        nmap[m["name"].lower()] = m
        first = m["name"].split()[0].lower()
        if first not in nmap: nmap[first] = m

    found, seen = [], set()
    for name, mem in sorted(nmap.items(), key=lambda x: -len(x[0])):
        if id(mem) in seen: continue
        match = re.search(rf"\b{re.escape(name)}\b", tl)
        if match:
            found.append((match.start(), mem))
            seen.add(id(mem))
    found.sort(key=lambda x: x[0])
    if len(found) < 2: return None, None

    recip = None
    m = re.search(r"\bto\s+(\w+)", tl)
    if m: recip = nmap.get(m.group(1))
    if not recip:
        m = re.search(r"(\w+)\s+ko\b", tl)
        if m: recip = nmap.get(m.group(1))
    if not recip: return None, None

    subject = next((mem for _, mem in found if mem["email"] != recip["email"]), None)
    return subject, recip["email"]


# ═══════════════════════════════════════════════════════════════════════════════
# TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@tool
def get_user_updates(user_name: str, days: Union[int, str] = 1) -> str:
    """Get last N days of updates for a specific team member. Leaders only."""
    err = _check_leader()
    if err: return err
    if not _user_in_own_team(user_name):
        return f"Access denied: '{user_name}' is not in your team."
    days = max(int(days), 1)
    user = get_user_by_name(user_name)
    if not user: return f"No user found: '{user_name}'."
    updates = get_updates_by_user_and_days(user["id"], days)
    if days == 1:
        today = str(dt_date.today())
        updates = [u for u in updates if str(u["date"]) == today]
    if not updates:
        period = "today" if days == 1 else f"last {days} day(s)"
        return f"No updates found for {_format_name(user['name'])} ({period})."
    divider = "─" * 44
    parts = []
    for u in updates:
        parts.append(_format_update(user["name"], str(u["date"]), u["content"]))
        if len(updates) > 1:
            parts.append(divider)
    return "\n".join(parts).strip()


@tool
def get_team_updates(date: Optional[str] = None) -> str:
    """Get all submitted updates for your entire team. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
    all_members = get_users_by_team(team["id"])
    total = len(all_members)
    submitted_count = len(rows)
    missing = get_missing_users_today(team["id"], target)

    if not rows:
        pending_names = ", ".join(_format_name(_row_get(u, "user_name", "?")) for u in missing)
        return (
            f"No updates submitted for '{_format_name(team['name'])}' on {_format_date(target)}.\n\n"
            f"⏳ Pending ({total}/{total}): {pending_names}"
        )

    result = _format_team_block(team["name"], target, rows)
    result += f"\n\n**📊 {submitted_count} of {total} members submitted**"
    if missing:
        pending_names = " • ".join(_format_name(_row_get(u, "user_name", "?")) for u in missing)
        result += f"\n⏳ Still pending: {pending_names}"
    return result


@tool
def get_missing_updates(date: Optional[str] = None) -> str:
    """List members who have NOT submitted today. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    missing = get_missing_users_today(team["id"], target)
    if not missing:
        return "\n".join([
            f"**✅ {_format_name(team['name'])} — All Updates Submitted**",
            "",
            f"Date : {_format_date(target)}",
            "",
            "Every member has submitted their update.",
        ])

    lines = [
        f"**⏳ {_format_name(team['name'])} — Pending Updates ({_format_date(target)})**",
        "",
        "| # | Name | Email |",
        "|:-:|------|-------|",
    ]
    for i, u in enumerate(missing, 1):
        name  = _format_name(_row_get(u, "user_name", "?"))
        email = _row_get(u, "email", "no email")
        lines.append(f"| {i} | {name} | {email} |")

    lines += [
        "",
        f"Total pending : {len(missing)}",
    ]
    return "\n".join(lines)


@tool
def get_meeting_notes_tool(date: Optional[str] = None) -> str:
    """Fetch meeting notes / MoM for your team. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    notes = db_get_meeting_notes(team["id"], target)
    if not notes:
        return f"No meeting notes found for '{team['name']}' on {_format_date(target)}."
    return "\n".join([
        f"📝  Meeting Notes — {team['name']}",
        f"📅  {_format_date(target)}",
        "",
        _strip_html(notes["content"]),
    ])


@tool
def get_team_members_info() -> str:
    """List all members of your team. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    members = get_users_by_team(team["id"])
    if not members: return f"No members found in '{team['name']}'."

    leaders = [m for m in members if (m["role"] or "").lower() == "leader"]
    regular = [m for m in members if (m["role"] or "").lower() != "leader"]
    sorted_members = leaders + regular

    lines = [
        f"**👥 {_format_name(team['name'])} — Team Members**",
        "",
        "| # | &nbsp; | Name | Role | Email |",
        "|:-:|:-----:|------|:----:|-------|",
    ]
    for i, m in enumerate(sorted_members, 1):
        role = (m["role"] or "member").capitalize()
        icon = "👑" if role.lower() == "leader" else "👤"
        name = _format_name(m["name"])
        lines.append(f"| {i} | {icon} | {name} | {role} | {m['email']} |")

    lines += [
        "",
        f"Total : {len(members)}  |  👑 Leaders : {len(leaders)}  |  👤 Members : {len(regular)}",
    ]
    return "\n".join(lines)


def _build_email_body(target, inc_u=True, inc_m=True, days=1):
    team = _own_team()
    if not team: return None

    sections = []
    days = max(int(days), 1)

    # ── Daily Updates ──────────────────────────────────────────────────────
    if inc_u:
        if days == 1:
            # Single date
            rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
            date_groups = [(target, rows)] if rows else []
            missing = get_missing_users_today(team["id"], target)
        else:
            # Date range: from (target - days + 1) to target
            from_date = (dt_date.fromisoformat(target) - timedelta(days=days - 1)).isoformat()
            all_rows = get_team_updates_by_date_range(team["id"], from_date, target)
            # Group by date
            grouped: dict = {}
            for r in all_rows:
                d = r["date"]
                grouped.setdefault(d, []).append(r)
            # Sort dates newest first
            date_groups = sorted(grouped.items(), key=lambda x: x[0], reverse=True)
            missing = []  # not shown for multi-day

        update_cards = ""
        if date_groups:
            for date_key, date_rows in date_groups:
                # Date sub-header for multi-day
                date_header = (
                    f'<div style="font-size:13px;font-weight:700;color:#6b7280;'
                    f'margin:16px 0 8px;text-transform:uppercase;letter-spacing:.5px;">'
                    f'📅 {_format_date(date_key)}</div>'
                    if days > 1 else ""
                )
                cards = ""
                for r in date_rows:
                    plain = _strip_html(r["content"]).replace("\n", "<br>")
                    cards += f"""
                    <div style="margin-bottom:14px;padding:14px 16px;background:#f8f9fa;
                                border-left:4px solid #4f46e5;border-radius:6px;">
                      <div style="font-weight:700;color:#1f2937;font-size:14px;
                                  margin-bottom:6px;">
                        {_format_name(r['user_name'])}
                        <span style="font-weight:400;color:#6b7280;font-size:12px;
                                     margin-left:8px;">({_row_get(r, 'user_role', 'member')})</span>
                      </div>
                      <div style="color:#374151;font-size:13px;line-height:1.7;">{plain}</div>
                    </div>"""
                update_cards += date_header + cards
        else:
            update_cards = '<p style="color:#6b7280;font-style:italic;">No updates found.</p>'

        missing_html = ""
        if missing:
            names_li = "".join(
                f'<li style="color:#92400e;margin-bottom:4px;">'
                f'{_format_name(_row_get(u,"user_name","?"))} '
                f'&lt;{_row_get(u,"email","")}&gt;</li>'
                for u in missing
            )
            missing_html = f"""
            <div style="margin-top:16px;padding:14px 16px;background:#fffbeb;
                        border:1px solid #fcd34d;border-radius:6px;">
              <div style="font-weight:700;color:#92400e;margin-bottom:8px;">
                ⚠&nbsp; Missing Submissions ({len(missing)})
              </div>
              <ul style="margin:0;padding-left:20px;">{names_li}</ul>
            </div>"""

        sections.append(f"""
        <div style="margin-bottom:28px;">
          <h2 style="font-size:16px;font-weight:700;color:#4f46e5;
                     border-bottom:2px solid #e5e7eb;padding-bottom:6px;margin-bottom:12px;">
            📋&nbsp; Daily Updates
          </h2>
          {update_cards}
          {missing_html}
        </div>""")

    # ── Meeting Notes ──────────────────────────────────────────────────────
    if inc_m:
        notes = db_get_meeting_notes(team["id"], target)
        if notes:
            mom_content = _strip_html(notes["content"]).replace("\n", "<br>")
            mom_html = f'<div style="color:#374151;font-size:14px;line-height:1.7;">{mom_content}</div>'
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
        "daily updates and meeting notes" if inc_u and inc_m
        else "daily updates" if inc_u
        else "meeting notes"
    )

    if days > 1:
        from_date = (dt_date.fromisoformat(target) - timedelta(days=days - 1)).isoformat()
        date_label = f"{_format_date(from_date)} – {_format_date(target)}"
    else:
        date_label = _format_date(target)

    body_html = "".join(sections)
    return f"""<html-body>
    <p style="color:#374151;margin-bottom:24px;">
      Hi Team,<br><br>
      Please find the <strong>{intro}</strong> for
      <strong>{_format_name(team['name'])}</strong> — {date_label} below.
    </p>
    {body_html}
    </html-body>"""


@tool
def summarize_updates(date: Optional[str] = None) -> str:
    """Show combined summary of team updates and meeting notes. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    rows    = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
    missing = get_missing_users_today(team["id"], target)
    notes   = db_get_meeting_notes(team["id"], target)
    divider = "─" * 44
    out = []
    if rows:
        out.append(_format_team_block(team["name"], target, rows))
    else:
        out.append(f"No updates submitted for '{team['name']}' on {_format_date(target)}.")
    if missing:
        out.append(f"\n⏳  Pending ({len(missing)}):")
        for u in missing:
            out.append(f"  •  {_format_name(_row_get(u,'user_name','?'))}")
    if notes:
        out += ["", divider,
                f"📝  Minutes of Meeting — {_format_date(target)}", divider,
                _strip_html(notes["content"])]
    else:
        out.append(f"\n📝  No meeting notes for {_format_date(target)}.")
    return "\n".join(out).strip()


@tool
def send_email_report(
    to_email: str,
    subject: str = "",
    date: Optional[str] = None,
    content_type: str = "updates",
    days: Union[int, str] = 1,
) -> str:
    """Send team report email for one date or multiple days. Leaders only.
    Use days>1 for 'last N days' or 'previous' requests."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    days = max(int(days), 1)
    ct = (content_type or "updates").strip().lower()
    if ct in ("mom", "meeting_notes", "meeting-notes", "notes", "minutes"):
        inc_u, inc_m, kind = False, True, "Meeting Notes"
    elif ct in ("both", "all", "full", "summary"):
        inc_u, inc_m, kind = True, True, "Daily Updates & Meeting Notes"
    else:
        inc_u, inc_m, kind = True, False, "Daily Updates"
    body = _build_email_body(target, inc_u, inc_m, days=days)
    if not body: return "Team not found."
    if not subject.strip():
        subject = f"{kind} — {team['name']} — {_format_date(target)}" if days == 1 else \
                  f"{kind} — {team['name']} — Last {days} Days"
    cc = [e for e in get_team_members_emails(team["id"]) if e.lower() != to_email.lower()]
    ok, msg = send_email(to_email, subject, body, list(dict.fromkeys(cc)))
    return (
        f"✅  Email sent to {to_email}  ({kind})."
        if ok else f"❌  Failed to send: {msg}"
    )


@tool
def send_missing_update_reminders(
    date: Optional[str] = None,
    manager_email: Optional[str] = None,
) -> str:
    """Send reminders to members who haven't submitted. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    sent, failed = [], []
    for u in get_missing_users_today(team["id"], target):
        email = _row_get(u, "email")
        name  = _row_get(u, "user_name", "there")
        if not email:
            failed.append(f"{_format_name(name)} (no email on record)")
            continue
        subj = f"Reminder: Please submit your daily update for {_format_date(target)}"
        body = (
            f"Hi {_format_name(name)},\n\n"
            f"This is a friendly reminder to submit your daily update for {_format_date(target)}.\n\n"
            f"Please log in and add your update as soon as possible.\n\n"
            f"Thanks,\nTeam Tracker"
        )
        cc = (
            [manager_email.strip()]
            if manager_email and manager_email.strip().lower() != email.lower()
            else []
        )
        ok, msg = send_email(email, subj, body, cc)
        entry = f"{_format_name(name)} <{email}>"
        (sent if ok else failed).append(entry if ok else f"{entry} ({msg})")
    if not sent and not failed:
        return f"✅ No reminders needed — everyone submitted for {_format_date(target)}."

    lines = [
        f"**📧 {_format_name(team['name'])} — Reminder Sent ({_format_date(target)})**",
        "",
    ]

    if sent:
        lines += [
            f"✅ Sent ({len(sent)})",
            "",
            "| # | Name | Email |",
            "|:-:|------|-------|",
        ]
        for i, s in enumerate(sent, 1):
            # s format: "Full Name <email>"
            m = re.match(r"^(.+?)\s*<(.+?)>$", s)
            name_s  = m.group(1).strip() if m else s
            email_s = m.group(2).strip() if m else ""
            lines.append(f"| {i} | {name_s} | {email_s} |")
        lines.append("")

    if failed:
        lines += [
            f"❌ Failed ({len(failed)})",
            "",
            "| # | Name | Reason |",
            "|:-:|------|--------|",
        ]
        for i, f_ in enumerate(failed, 1):
            m = re.match(r"^(.+?)\s*<(.+?)>\s*\((.+?)\)$", f_)
            if m:
                lines.append(f"| {i} | {m.group(1).strip()} | {m.group(3).strip()} |")
            else:
                lines.append(f"| {i} | {f_} | — |")
        lines.append("")

    if manager_email:
        lines.append(f"CC : {manager_email}")

    return "\n".join(lines)


@tool
def get_standup_digest(date: Optional[str] = None) -> str:
    """Standup snapshot: submitted, pending, MoM. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    rows    = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
    missing = get_missing_users_today(team["id"], target)
    notes   = db_get_meeting_notes(team["id"], target)
    divider = "─" * 44
    out = [
        f"📋  Standup Digest — {team['name']}",
        f"📅  {_format_date(target)}",
        divider,
        f"  ✅  Submitted : {len(rows)}",
        f"  ⏳  Pending   : {len(missing)}",
        f"  📝  MoM       : {'✅ Available' if notes else '❌ Not added'}",
        divider,
    ]
    if rows:
        out.append("\n✅  Submitted:")
        for r in rows:
            preview = _strip_html(r["content"]).replace("\n", " ").strip()
            out.append(
                f"  •  {_format_name(r['user_name'])}  —  "
                f"{preview[:80] + '...' if len(preview) > 80 else preview}"
            )
    if missing:
        out.append("\n⏳  Pending:")
        for u in missing:
            out.append(
                f"  •  {_format_name(_row_get(u,'user_name','?'))}  "
                f"({_row_get(u,'email','no email')})"
            )
    return "\n".join(out).strip()


@tool
def send_missing_list_email(to_email: str, date: Optional[str] = None) -> str:
    """Send the list of members who have NOT submitted their update today, to an email. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    missing = get_missing_users_today(team["id"], target)
    if not missing:
        return f"✅  Everyone in '{_format_name(team['name'])}' has submitted their update for {_format_date(target)}. No email sent."

    rows_html = ""
    for i, u in enumerate(missing, 1):
        name = _format_name(_row_get(u, "user_name", "?"))
        email = _row_get(u, "email", "—")
        rows_html += (
            f"<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;'>{i}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;'>⏳ {name}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;'>{email}</td>"
            f"</tr>"
        )

    body = (
        f"<html-body>"
        f"<p style='color:#374151;margin-bottom:20px;'>Hi,<br><br>"
        f"The following members of <strong>{_format_name(team['name'])}</strong> have "
        f"<strong>not submitted</strong> their update for <strong>{_format_date(target)}</strong>.</p>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
        f"<thead><tr style='background:#dc2626;color:#ffffff;'>"
        f"<th style='padding:10px 12px;'>#</th>"
        f"<th style='padding:10px 12px;text-align:left;'>Name</th>"
        f"<th style='padding:10px 12px;text-align:left;'>Email</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
        f"<p style='color:#6b7280;font-size:12px;margin-top:16px;'>Total pending: {len(missing)}</p>"
        f"</html-body>"
    )
    subject = f"Missing Updates — {_format_name(team['name'])} — {_format_date(target)}"
    ok, msg = send_email(to_email, subject, body, [])
    return (
        f"✅  Missing members list ({len(missing)} pending) sent to {to_email}."
        if ok else f"❌  Failed: {msg}"
    )


@tool
def send_member_list_email(to_email: str) -> str:
    """Send team member list (names, roles, emails) to an email address. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    members = get_users_by_team(team["id"])
    if not members: return f"No members found in '{team['name']}'."

    leaders = [m for m in members if (m["role"] or "").lower() == "leader"]
    regular = [m for m in members if (m["role"] or "").lower() != "leader"]
    sorted_members = leaders + regular

    rows_html = ""
    for i, m in enumerate(sorted_members, 1):
        role = (m["role"] or "member").capitalize()
        icon = "👑" if role.lower() == "leader" else "👤"
        rows_html += (
            f"<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;'>{i}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;'>{icon} {_format_name(m['name'])}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center;'>{role}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #e5e7eb;'>{m['email']}</td>"
            f"</tr>"
        )

    body = (
        f"<html-body>"
        f"<p style='color:#374151;margin-bottom:20px;'>Hi,<br><br>"
        f"Please find the member list for team <strong>{_format_name(team['name'])}</strong> below.</p>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
        f"<thead><tr style='background:#4f46e5;color:#ffffff;'>"
        f"<th style='padding:10px 12px;'>#</th>"
        f"<th style='padding:10px 12px;text-align:left;'>Name</th>"
        f"<th style='padding:10px 12px;'>Role</th>"
        f"<th style='padding:10px 12px;text-align:left;'>Email</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table>"
        f"<p style='color:#6b7280;font-size:12px;margin-top:16px;'>"
        f"Total: {len(members)} | 👑 Leaders: {len(leaders)} | 👤 Members: {len(regular)}</p>"
        f"</html-body>"
    )
    subject = f"Team Member List — {_format_name(team['name'])}"
    ok, msg = send_email(to_email, subject, body, [])
    return (
        f"✅  Member list for '{_format_name(team['name'])}' sent to {to_email}."
        if ok else f"❌  Failed: {msg}"
    )


@tool
def send_user_updates_email(
    user_name: str,
    to_email: str,
    days: Union[int, str] = 1,
    target_date: Optional[str] = None,
) -> str:
    """Send a specific member's updates to an email address. Leaders only.
    Use target_date (YYYY-MM-DD) to send updates for one specific date (e.g. yesterday).
    Use days>1 for a multi-day range ending today."""
    err = _check_leader()
    if err: return err
    if not _user_in_own_team(user_name):
        return f"'{_format_name(user_name)}' is not in your team."
    days = max(int(days), 1)
    user = get_user_by_name(user_name)
    if not user: return f"No user found: '{user_name}'."

    updates = get_updates_by_user_and_days(user["id"], days)

    if target_date:
        # Specific date requested (e.g. yesterday) — filter to that date only
        updates = [u for u in updates if str(u["date"]) == target_date]
        period_label = _format_date(target_date)
        sent_label = _format_date(target_date)
        subj_date = _format_date(target_date)
    elif days == 1:
        # Today only
        today = str(dt_date.today())
        updates = [u for u in updates if str(u["date"]) == today]
        period_label = "today's"
        sent_label = "today"
        subj_date = _format_date(today)
    else:
        period_label = f"last {days} days"
        sent_label = f"last {days} days"
        subj_date = f"Last {days} Days"

    if not updates:
        return f"No updates found for {_format_name(user['name'])} for {period_label}."

    cards = ""
    for u in updates:
        plain = _strip_html(u["content"]).replace("\n", "<br>")
        date_label = _format_date(u["date"])
        cards += f"""
        <div style="margin-bottom:14px;padding:14px 16px;background:#f8f9fa;
                    border-left:4px solid #4f46e5;border-radius:6px;">
          <div style="font-weight:700;color:#1f2937;font-size:14px;margin-bottom:6px;">
            📅 {date_label}
          </div>
          <div style="color:#374151;font-size:13px;line-height:1.7;">{plain}</div>
        </div>"""

    member_name = _format_name(user["name"])
    body = f"""<html-body>
    <p style="color:#374151;margin-bottom:20px;">
      Hi,<br><br>
      Please find below the {period_label} update from <strong>{member_name}</strong>.
    </p>
    {cards}
    </html-body>"""
    subj = f"{member_name}'s Update — {subj_date}"
    ok, msg = send_email(to_email, subj, body, [])
    return (
        f"✅  {member_name}'s {sent_label} update sent to {to_email}."
        if ok else f"❌  Failed: {msg}"
    )


TOOLS = [
    get_user_updates, get_team_updates, get_missing_updates,
    get_meeting_notes_tool, get_team_members_info, summarize_updates,
    send_email_report, send_missing_update_reminders,
    get_standup_digest, send_user_updates_email, send_member_list_email,
    send_missing_list_email,
]
_TOOL_MAP = {t.name: t for t in TOOLS}


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

_SYS = """You are Team Tracker — a smart assistant for team leaders.
Today : {today}
User  : {profile}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Only answer team-related questions. Off-topic → "I can only help with team-related questions."
2. Use EXACTLY ONE tool per query.
3. Only leaders/managers may call tools.
4. If intent is unclear, ask ONE short clarifying question.
5. Never expose raw errors — return a friendly message.
6. Support Hinglish queries naturally.
7. Confirm after every send/email action.
8. Keep replies concise — no bullet lists of commands.
9. If no data found, reply in one short line.
10. Never list capabilities unless user types 'help'.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL SELECTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SHOW UPDATES
  show/give [name] update              → get_user_updates(user_name, days=1)
  show [name] last N days update       → get_user_updates(user_name, days=N)
  show [name] yesterday / kal update   → get_user_updates(user_name, days=2)
  show all / sabka / team update       → get_team_updates()
  show yesterday team update           → get_team_updates(date=<yesterday ISO>)
  show 15 april team update            → get_team_updates(date="2026-04-15")

STATUS & MEMBERS
  who not updated / missing / pending  → get_missing_updates()
  did [name] update / kya [name] ne update ki → get_user_updates(user_name, days=1)
  how many updated / kitne log ne update ki  → get_missing_updates()
  list members / who is in team / member dikha → get_team_members_info()

MEETING NOTES
  meeting notes / MoM / metting not   → get_meeting_notes_tool()
  yesterday meeting notes / kal ki mom → get_meeting_notes_tool(date=<yesterday ISO>)
  15 april meeting notes               → get_meeting_notes_tool(date="2026-04-15")

SUMMARY & DIGEST
  summary / full report / poori summary → summarize_updates()
  standup digest / snapshot             → get_standup_digest()

SEND / EMAIL
  send [name] update to [person/email]      → send_user_updates_email(user_name, to_email, days=1)
  send [name] yesterday update to X        → send_user_updates_email(user_name, to_email, days=2)
  send [name] last N days update to X      → send_user_updates_email(user_name, to_email, days=N)
  send member list to [email/person]           → send_member_list_email(to_email)
  send list of who not updated to [email]      → send_missing_list_email(to_email)
  send today report / full report to [email] → send_email_report(to_email, content_type="updates", days=1)
  send summary to [email]                    → send_email_report(to_email, content_type="both")
  send last N days report to [email]         → send_email_report(to_email, days=N)
  remind missing / unko remind karo          → send_missing_update_reminders()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATE & DAYS — CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  today / aaj / abhi                    → days=1  (default)
  yesterday / kal / previous / purani / pichle → days=2
  last 3 days / 3 din                   → days=3
  last week / hafte                     → days=7
  specific date ("15 april 2026", "april 15", "15/04/2026") → pass as ISO date string YYYY-MM-DD
  Always pass days or date when user mentions any time period.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"show ankur update"                      → get_user_updates(user_name="Ankur", days=1)
"last 3 days ankur update"              → get_user_updates(user_name="Ankur", days=3)
"ankur kal ki update"                   → get_user_updates(user_name="Ankur", days=2)
"show all member update today"          → get_team_updates()
"show yesterday team update"            → get_team_updates(date=<yesterday ISO>)
"show 15 april team update"             → get_team_updates(date="2026-04-15")
"did harish update today"               → get_user_updates(user_name="Harish", days=1)
"kya ankur ne update ki"                → get_user_updates(user_name="Ankur", days=1)
"how many updated today / kitne ne update ki" → get_missing_updates()
"who is in team / member dikha"         → get_team_members_info()
"send ankur update to tarun"            → send_user_updates_email(user_name="Ankur", to_email=<tarun email>, days=1)
"send ankur yesterday update to tarun"  → send_user_updates_email(user_name="Ankur", to_email=<tarun email>, days=2)
"send last 3 days report to x@y.com"   → send_email_report(to_email="x@y.com", days=3)
"purani updates bhejo x@y.com ko"      → send_email_report(to_email="x@y.com", days=2)
"meeting notes / metting not"           → get_meeting_notes_tool()
"kal ki meeting notes"                  → get_meeting_notes_tool(date=<yesterday ISO>)
"15 april meeting notes"                → get_meeting_notes_tool(date="2026-04-15")
"who didn't update / kon nahi kiya"    → get_missing_updates()
"remind missing / unko reminder bhejo" → send_missing_update_reminders()
"standup digest"                        → get_standup_digest()
"how to cook pasta?"                    → "I can only help with team-related questions."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TONE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - Concise and friendly.
  - ✅ success   ❌ error   ⏳ pending
  - Match user's language — Hinglish if they write Hinglish.
  - Never suggest commands or list features unless asked.
  - One line is enough for confirmations."""


# ═══════════════════════════════════════════════════════════════════════════════
# PATTERN LISTS
# ═══════════════════════════════════════════════════════════════════════════════

_SEND_W   = ["send","sent","mail","email","forward","share","bhej","bhejo","bheja"]
_READ_W   = ["list","show","give me","tell me","what is","who is","display","view"]
_REMIND_P = ["remind missing","ping pending","ping missing","remind pending",
             "send reminder","reminder email","send mail to those who","mail those who didn",
             "remind people who","remind users who","jisne update nahi","update nahi ki",
             "use mail karo","unko mail karo","use reminder"]
_NAMES_P  = ["who updates today","who update today","who updated today","who has updated today",
             "who submitted today","who gave update today","kisne update di","kisne update kiya",
             "who updates","who update","who updated","who gave update","who sent update",
             "who done update","who did update","kon update","kaun update"]

# ✅ FIXED: removed generic words — only personal pronouns remain
_MY_P     = ["my update","my today update","meri update","mera update","my progress","my report",
             "show my update","give me my update","my work","apna update","apni update",
             "aaj mera update","aaj ki meri update"]

# words that confirm "MY own" update (not someone else's)
_PERSONAL = ["my update","my today","meri update","mera update","my progress","my report",
             "show my","give me my","my work","apna update","apni update",
             "aaj mera","aaj ki meri"]

_DIGEST_P = ["standup digest","stand-up digest","stand up digest","today's snapshot",
             "todays snapshot","who is pending","list who updated","submitted today"]
_MISS_P   = ["not update","didn't update","didnt update","hasn't update","not submit",
             "didn't submit","pending","missing","nahi ki","nahi di","ni ki",
             "who not update","who didn't update","who have not","who has not",
             "kon nahi","kaun nahi",
             "not udpate","not updaet","not updte","not updat",
             "list who not","send list who","jisne nahi","jinhone nahi","jinke nahi",
             "jisne update nahi","jinhone update nahi"]
_SUM_P    = ["summary","summarize","summarise","all updates","full report","team report",
             "team summary","team update","all update","sab updates","sari updates","poori summary"]
_TODAY_P  = ["updates today","today's update","todays update","team update today",
             "all member update","all members update","give me all member","sabka update",
             "sab ka update","all team update","everyone's update","everyone update",
             "sare members","poore team"]
_MOM_P    = ["meeting note","meeting notes","meeting-note","m.o.m","minutes of meeting",
             "meeting minute","mins of meeting","team notes","daily notes",
             "metting not","metting note","metting notes","meetng note"]
_MEMLIST_P = ["show member","list member","team member","member list","member dikha",
              "members dikha","who is in team","team mein kaun","team me kaun","team ke member",
              "apne team ka member","team ka member","show me member","list me member",
              "give me member","who are members","list team","team list"]
_COUNT_P   = ["how many","kitne","count update","total submitted","kitne log","kitne member",
              "how many member","how many update","total update","kitne ne","how many ne"]
_STATUS_P  = ["did","has he","has she","ne update ki","ne update kiya","ne submit",
              "did update","did he update","did she update","kya update","ne aaj update"]
_TEAM_KW  = ["update","submit","missing","pending","remind","digest","standup","meeting","mom",
             "note","member","team","email","send","mail","report","summary","leader","manager",
             "who","list","show","give","tell","bhej","kon","kaun",
             "how","many","kitne","did","kya","check","count","status","kal","yesterday",
             "dikha","batao","bata","ne","submitted","total"]


# ═══════════════════════════════════════════════════════════════════════════════
# SHORTCUT ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

def _try_shortcut(user_input: str) -> Optional[str]:
    global _last_context, _last_recipient, _last_user_sent
    text = user_input.lower().strip()

    # ── greetings ──────────────────────────────────────────────────────────────
    _GREET = ["hi","hello","hey","hii","helo","namaste","good morning","good afternoon",
              "good evening","howdy","sup","what's up","whats up"]
    if any(text == g or text.startswith(g + " ") for g in _GREET):
        name = (_row_get(_current_user, "name", "") or "").split()[0]
        return f"Hi {_format_name(name)}! 👋" if name else "Hello! 👋"

    # ── help ───────────────────────────────────────────────────────────────────
    if text in ("help", "?", "kya kar sakte ho", "what can you do"):
        return (
            "Here's what I can do:\n"
            "  •  show [name] update\n"
            "  •  send [name] update to [person]\n"
            "  •  who is missing today\n"
            "  •  remind missing users\n"
            "  •  standup digest\n"
            "  •  meeting notes / MoM\n"
            "  •  send full report to [email]"
        )

    # ── resend ─────────────────────────────────────────────────────────────────
    if _has(text, ["send again","phir se bhej","dobara bhej","resend",
                   "again send","send it again","bhej do phir"]):
        if _last_recipient:
            if _last_user_sent:
                return send_user_updates_email.invoke(
                    {"user_name": _last_user_sent, "to_email": _last_recipient}
                )
            ct = {"mom": "mom", "summary": "both"}.get(_last_context, "updates")
            return send_email_report.invoke({"to_email": _last_recipient, "content_type": ct})
        return "No previous email found to resend."

    # ── email lookup ───────────────────────────────────────────────────────────
    _EQ = ["mail of","email of","mail id of","email id of","ka email","ki email",
           "ka mail","ki mail","give me mail","give me email","what is mail","what is email"]
    if _has(text, _EQ):
        m = _find_member_in_text(text, _all_candidates())
        if m: return f"📧  {_format_name(m['name'])}'s email:  {m['email']}"
        return "Person not found in your team."

    # ── off-topic guard ────────────────────────────────────────────────────────
    if not any(kw in text for kw in _TEAM_KW):
        return "I can only help with team-related questions."

    has_send   = _has(text, _SEND_W)
    has_update = "update" in text
    is_read    = _has(text, _READ_W)
    asks_miss  = _has(text, _MISS_P)
    asks_sum   = _has(text, _SUM_P)
    asks_mom   = _has(text, _MOM_P) or bool(re.search(r'\bmom\b', text))
    asks_today  = _has(text, _TODAY_P)
    asks_all    = bool(re.search(r'\ball\s+mem', text)) or _has(text, ["sabka","sab ka","sare member","everyone"])
    asks_remind = _has(text, _REMIND_P)
    asks_digest = _has(text, _DIGEST_P)
    asks_names  = _has(text, _NAMES_P)
    asks_my     = _has(text, _MY_P)
    asks_mlist  = _has(text, _MEMLIST_P)
    asks_count  = _has(text, _COUNT_P)
    asks_status = _has(text, _STATUS_P) and not has_send

    raw_email = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", user_input)
    recip = raw_email.group(0) if raw_email else None
    if has_send and not recip:
        if re.search(r"\b(manager|boss)\b", text):
            mgrs = get_managers()
            if mgrs: recip = mgrs[0]["email"]
        if not recip:
            recip = _email_after_keyword(text) or _email_from_text(text)

    # ── "send today to X" — explicit today team report (no member name) ──────────
    if has_send and recip and _has_today(text) and not asks_miss:
        team = _own_team()
        matched_member = _find_member_in_text(text, get_users_by_team(team["id"])) if team else None
        if not matched_member:
            # No member name → send full team today report
            _last_context = "updates"; _last_user_sent = None; _last_recipient = recip
            return send_email_report.invoke({"to_email": recip, "content_type": "updates", "days": 1})

    # ── follow-up "send this to X" ─────────────────────────────────────────────
    if has_send and recip and _last_context and not asks_mlist and not asks_miss and (_has_ref(text) or not has_update):
        _days = _detect_days(text)
        if _last_user_sent:
            _last_recipient = recip
            return send_user_updates_email.invoke(
                {"user_name": _last_user_sent, "to_email": recip, "days": _days}
            )
        ct = {"mom": "mom", "summary": "both"}.get(_last_context, "updates")
        _last_recipient = recip
        return send_email_report.invoke({"to_email": recip, "content_type": ct, "days": _days})

    # ── meeting notes ──────────────────────────────────────────────────────────
    if asks_mom:
        _mom_date = _extract_date(text) or (_yesterday_date() if _is_yesterday_only(text) else None)
        if has_send and recip:
            _last_context = "mom"; _last_user_sent = None; _last_recipient = recip
            args = {"to_email": recip, "content_type": "mom"}
            if _mom_date: args["date"] = _mom_date
            return send_email_report.invoke(args)
        _last_context = "mom"; _last_user_sent = None
        args = {}
        if _mom_date: args["date"] = _mom_date
        return get_meeting_notes_tool.invoke(args)

    # ── who is leader / manager ────────────────────────────────────────────────
    asks_q      = _has(text, ["who","what","tell","show","list","give me","kon","kaun"])
    asks_leader = _has(text, ["leader","team lead","team-lead","head","admin"])
    asks_mgr    = _has(text, ["manager","boss"])
    if asks_q and (asks_leader or asks_mgr) and not has_send:
        team = _own_team(); people = []
        if team and asks_leader:
            people = [m for m in get_users_by_team(team["id"])
                      if (m["role"] or "").lower() in
                      ("leader","lead","team_leader","team-leader","admin")]
        if asks_mgr: people.extend(get_managers())
        seen, unique = set(), []
        for p in people:
            if p["email"] not in seen:
                seen.add(p["email"]); unique.append(p)
        _last_context = "leader"; _last_user_sent = None
        if not unique:
            label = "manager" if asks_mgr and not asks_leader else "leader"
            return f"No {label} found."
        label = ("Managers:" if asks_mgr and not asks_leader
                 else f"Leaders of '{_own_team_name()}':")
        return "\n".join(
            [label] + [f"  •  {_format_name(p['name'])}  ({p['role']})  —  {p['email']}"
                       for p in unique]
        )

    # ── send missing members list to email ───────────────────────────────────
    if asks_miss and has_send and recip:
        _last_context = "missing"; _last_user_sent = None; _last_recipient = recip
        _miss_date = _extract_date(text) or (_yesterday_date() if _is_yesterday_only(text) else None)
        args = {"to_email": recip}
        if _miss_date: args["date"] = _miss_date
        return send_missing_list_email.invoke(args)

    # ── send member list to email ─────────────────────────────────────────────
    if asks_mlist and has_send and recip:
        _last_context = "members"; _last_user_sent = None; _last_recipient = recip
        return send_member_list_email.invoke({"to_email": recip})

    # ── team members list ─────────────────────────────────────────────────────
    if asks_mlist and not has_send:
        _last_context = "members"; _last_user_sent = None
        return get_team_members_info.invoke({})

    # ── how many updated today ────────────────────────────────────────────────
    if asks_count and not has_send:
        team = _own_team()
        if team:
            target_d = _extract_date(text) or (_yesterday_date() if _is_yesterday_only(text) else str(dt_date.today()))
            rows = [r for r in get_all_teams_updates_by_date(target_d) if r["team_id"] == team["id"]]
            all_members = get_users_by_team(team["id"])
            pending = len(all_members) - len(rows)
            return (
                f"**📊 {_format_name(team['name'])} — {_format_date(target_d)}**\n\n"
                f"✅  Submitted : {len(rows)}\n"
                f"⏳  Pending   : {pending}\n"
                f"👥  Total     : {len(all_members)}"
            )

    # ── send summary ───────────────────────────────────────────────────────────
    if has_send and asks_sum and recip:
        _last_context = "summary"; _last_user_sent = None; _last_recipient = recip
        _days = _detect_days(text)
        return send_email_report.invoke({"to_email": recip, "content_type": "both", "days": _days})

    # ── my own update ──────────────────────────────────────────────────────────
    if asks_my and not has_send:
        # only show own update if personal pronoun is present
        has_personal = _has(text, _PERSONAL)
        if not has_personal:
            return "Kis member ki update chahiye? Naam batao (e.g. 'give me Harish update')"
        me = _row_get(_current_user, "name")
        if not me: return "Could not identify your account."
        user = get_user_by_name(me)
        if not user: return "Your account was not found."
        today = str(dt_date.today())
        ups = [u for u in get_updates_by_user_and_days(user["id"], 1)
               if str(u["date"]) == today]
        if not ups:
            return f"You haven't submitted an update today ({_format_date(today)}) yet."
        _last_context = "updates"; _last_user_sent = me
        return _format_update(me, today, ups[0]["content"])

    # ── today's team updates ───────────────────────────────────────────────────
    if (asks_today or asks_all) and not has_send:
        _last_context = "updates"; _last_user_sent = None
        return get_team_updates.invoke({})

    # ── date-specific team updates (yesterday / specific date, no member name) ─
    if not has_send and not asks_miss and not asks_names:
        spec_date = _extract_date(text) or (_yesterday_date() if _is_yesterday_only(text) else None)
        if spec_date:
            team = _own_team()
            if team:
                matched = _find_member_in_text(text, get_users_by_team(team["id"]))
                if not matched:
                    _last_context = "updates"; _last_user_sent = None
                    return get_team_updates.invoke({"date": spec_date})

    # ── did [member] update? (yes/no status check) ────────────────────────────
    if asks_status:
        team = _own_team()
        if team:
            matched = _find_member_in_text(text, get_users_by_team(team["id"]))
            if matched:
                target_d = _extract_date(text) or (_yesterday_date() if _is_yesterday_only(text) else str(dt_date.today()))
                rows = [r for r in get_all_teams_updates_by_date(target_d) if r["team_id"] == team["id"]]
                submitted = any(r["user_name"].lower() == matched["name"].lower() for r in rows)
                name = _format_name(matched["name"])
                if submitted:
                    return f"✅ {name} ne {_format_date(target_d)} ki update submit kar di hai."
                else:
                    return f"⏳ {name} ne {_format_date(target_d)} ki update abhi tak submit **nahi** ki."

    # ── show one member's update ───────────────────────────────────────────────
    if has_update and not has_send and not asks_sum and not asks_miss and not asks_names and not asks_all and not asks_status:
        team = _own_team()
        if team:
            matched = _find_member_in_text(text, get_users_by_team(team["id"]))
            if matched and any(kw in text for kw in [
                "update","updates","progress","report","work",
                "ka update","ki update","ke update",
            ]):
                _last_context = "updates"; _last_user_sent = matched["name"]
                days = _detect_days(text)
                if _wants_previous_only(text):
                    user = get_user_by_name(matched["name"])
                    if user:
                        all_ups = get_updates_by_user_and_days(user["id"], days)
                        prev = [u for u in all_ups if str(u["date"]) != str(dt_date.today())]
                        if not prev:
                            return f"No previous update found for {_format_name(matched['name'])}."
                        u = prev[0]
                        return _format_update(matched["name"], str(u["date"]), u["content"])
                return get_user_updates.invoke({"user_name": matched["name"], "days": days})
            elif not matched:
                # no name found → ask
                return "Kis member ki update chahiye? Naam batao (e.g. 'give me Harish update')"

    # ── send one member's update ───────────────────────────────────────────────
    if has_send and has_update and recip and not is_read and not asks_miss and not asks_sum:
        team = _own_team()
        if team:
            members = get_users_by_team(team["id"])
            subject_mem, recip_email = _parse_send_intent(text, members)
            final_recip = recip_email or recip
            _days = _detect_days(text)
            # "yesterday/kal" → send only that specific date, not a range
            _tdate = _yesterday_date() if _is_yesterday_only(text) else None
            if subject_mem:
                _last_context = "updates"
                _last_recipient = final_recip
                _last_user_sent = subject_mem["name"]
                return send_user_updates_email.invoke(
                    {"user_name": subject_mem["name"], "to_email": final_recip,
                     "days": _days, "target_date": _tdate}
                )
            single = _find_member_in_text(text, members)
            if single and single["email"] != final_recip:
                _last_context = "updates"
                _last_recipient = final_recip
                _last_user_sent = single["name"]
                return send_user_updates_email.invoke(
                    {"user_name": single["name"], "to_email": final_recip,
                     "days": _days, "target_date": _tdate}
                )
            # No member name found — send full team report
            _last_context = "updates"; _last_user_sent = None; _last_recipient = final_recip
            return send_email_report.invoke({"to_email": final_recip, "content_type": "updates", "days": _days})

    # ── who updated ───────────────────────────────────────────────────────────
    if asks_names and not has_send and not asks_miss:
        team = _own_team()
        if not team: return "Team not found."
        target = str(dt_date.today())
        rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
        if not rows:
            return f"No one has submitted an update yet for {_format_date(target)}."
        _last_context = "digest"; _last_user_sent = None
        lines = [f"Submitted on {_format_date(target)}:"] + \
                [f"  ✅  {_format_name(r['user_name'])}" for r in rows]
        missing = get_missing_users_today(team["id"], target)
        if missing:
            lines += ["", f"⏳  Pending ({len(missing)}):"] + [
                f"  •  {_format_name(_row_get(u,'user_name','?'))}" for u in missing
            ]
        return "\n".join(lines)

    # ── missing ────────────────────────────────────────────────────────────────
    if asks_miss and not has_send:
        _last_context = "missing"; _last_user_sent = None
        return get_missing_updates.invoke({})

    # ── digest ─────────────────────────────────────────────────────────────────
    if (asks_digest or asks_remind) and not has_send:
        _last_context = "digest"; _last_user_sent = None
        return get_standup_digest.invoke({})

    # ── send reminder ──────────────────────────────────────────────────────────
    if asks_remind:
        m2 = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", user_input)
        _last_context = "reminder"; _last_user_sent = None
        return send_missing_update_reminders.invoke(
            {"manager_email": m2.group(0) if m2 else None}
        )

    # ── show summary ───────────────────────────────────────────────────────────
    if asks_sum and not has_send:
        _last_context = "summary"; _last_user_sent = None
        return summarize_updates.invoke({})

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# LLM FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _get_llm():
    import os
    key = os.getenv("GROQ_API_KEY") or dotenv_values(_ENV_PATH).get("GROQ_API_KEY")
    if not key: raise RuntimeError("GROQ_API_KEY not set in .env")
    return ChatGroq(
        model="llama-3.1-8b-instant",
        temperature=0,
        groq_api_key=key,
    ).bind_tools(TOOLS, tool_choice="auto")


def _run_llm(user_input: str, chat_history: list, profile: str) -> str:
    llm = _get_llm()
    sys_msg = SystemMessage(
        content=_SYS.format(today=str(dt_date.today()), profile=profile)
    )
    history = []
    for msg in list(chat_history)[-8:]:
        c = getattr(msg, "content", "")
        if isinstance(c, str) and len(c) > 2000:
            msg = msg.__class__(content=c[:2000] + "…[truncated]")
        history.append(msg)

    messages = [sys_msg] + history + [HumanMessage(content=user_input)]
    try:
        resp = llm.invoke(messages)
    except Exception as e:
        err = str(e)
        if "tool_use_failed" in err or "failed_generation" in err:
            fn_m = re.search(r"<function=(\w+)>", err)
            if fn_m:
                fn = _TOOL_MAP.get(fn_m.group(1))
                if fn:
                    try: return fn.invoke({})
                    except Exception as fe: return f"Tool error: {fe}"
        if "rate_limit" in err.lower() or "429" in err:
            m = re.search(r"try again in ([\w.]+)", err)
            wait = f" Try again in {m.group(1)}." if m else ""
            return f"⚠️  Rate limit hit.{wait}"
        return f"⚠️  Something went wrong: {err}"

    if getattr(resp, "tool_calls", None):
        tc = resp.tool_calls[0]
        fn = _TOOL_MAP.get(tc["name"])
        if fn:
            args = {}
            for k, v in tc["args"].items():
                if k == "date" and v == "": continue
                if k == "days":
                    try: v = max(int(v), 1)
                    except: v = 1
                args[k] = v
            try: return str(fn.invoke(args))
            except Exception as e: return f"Tool error: {e}"

    content = getattr(resp, "content", "") or ""
    if not content:
        return "I'm not sure what you meant. Can you rephrase?"
    return content


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run_chatbot_query(user_input: str, chat_history: list, user_info=None) -> str:
    global _current_user
    _current_user = user_info
    try:
        if not _is_leader(user_info):
            return "⛔  Access denied: only team leaders can use this assistant."
        sc = _try_shortcut(user_input)
        if sc is not None: return sc
        lines = [
            f"Name  : {_row_get(user_info,'name','?')}",
            f"Email : {_row_get(user_info,'email','?')}",
            f"Role  : {_row_get(user_info,'role','?')} (LEADER)",
        ]
        if _row_get(user_info, "team_name"):
            lines.append(f"Team  : {_row_get(user_info,'team_name')}")
        return _run_llm(user_input, chat_history, "\n".join(lines))
    finally:
        _current_user = None