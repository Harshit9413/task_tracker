import re
import html as html_module
from datetime import date as dt_date, datetime as dt_datetime
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

def _detect_days(text: str) -> int:
    m = re.search(r"(\d+)\s*day", text)
    if m: return max(int(m.group(1)), 1)
    if "week" in text: return 7
    if "last month" in text or "pichle mahine" in text: return 30
    if any(w in text for w in [
        "yesterday", "kal ", "previous", "prev ", "purana",
        "pichla", "pehle ka", "before today"
    ]): return 2
    return 1

def _wants_previous_only(text: str) -> bool:
    return any(w in text for w in [
        "previous", "prev ", "purana", "pichla", "pehle ka",
        "yesterday", "kal ", "before today", "last update",
        "pichli baar", "pichle din",
    ])


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
        f"📅  Date: {_format_date(date)}",
        f"👤  Employee: {_format_name(user_name)}",
        "",
    ]
    if not sections:
        out.append(plain)
        return "\n".join(out)

    for label, items in sections:
        out.append(f"{label}:")
        for item in items:
            out.append(f"  •  {item}")
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
    if not rows:
        return f"No updates submitted for '{team['name']}' on {_format_date(target)}."
    return _format_team_block(team["name"], target, rows)


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
        return f"✅  All members submitted their update for {_format_date(target)}."
    lines = [f"⏳  Pending updates — {team['name']}  ({_format_date(target)}):"]
    for u in missing:
        lines.append(f"  •  {_format_name(_row_get(u,'user_name','?'))}  "
                     f"({_row_get(u,'email','no email')})")
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
    divider = "─" * 44
    return "\n".join([
        f"📝  Meeting Notes — {team['name']}",
        f"📅  {_format_date(target)}",
        divider,
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
    lines = [f"👥  Members of '{team['name']}':"]
    for m in members:
        lines.append(f"  •  {_format_name(m['name'])}  ({m['role']})  —  {m['email']}")
    return "\n".join(lines)


def _build_email_body(target, inc_u=True, inc_m=True):
    team = _own_team()
    if not team: return None
    parts = []
    if inc_u:
        rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
        if rows:
            parts.append("\n\n".join(
                f"{_format_name(r['user_name'])}:\n{_strip_html(r['content'])}"
                for r in rows
            ))
        else:
            parts.append("(No updates submitted.)")
        missing = get_missing_users_today(team["id"], target)
        if missing:
            names = ", ".join(_format_name(_row_get(u, "user_name", "?")) for u in missing)
            parts.append(f"Note: {names} did not submit an update today.")
    if inc_m:
        notes = db_get_meeting_notes(team["id"], target)
        parts.append(
            "Minutes of Meeting:\n" + _strip_html(notes["content"])
            if notes else f"No meeting notes for {_format_date(target)}."
        )
    intro = (
        "daily updates and meeting notes" if inc_u and inc_m
        else "daily updates" if inc_u
        else "meeting notes"
    )
    return f"Hi Team,\n\nPlease find the {intro} below.\n\n" + "\n\n".join(parts)


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
) -> str:
    """Send team report email. Leaders only."""
    err = _check_leader()
    if err: return err
    team = _own_team()
    if not team: return "Team not found."
    target = date or str(dt_date.today())
    ct = (content_type or "updates").strip().lower()
    if ct in ("mom", "meeting_notes", "meeting-notes", "notes", "minutes"):
        inc_u, inc_m, kind = False, True, "Meeting Notes"
    elif ct in ("both", "all", "full", "summary"):
        inc_u, inc_m, kind = True, True, "Daily Updates & Meeting Notes"
    else:
        inc_u, inc_m, kind = True, False, "Daily Updates"
    body = _build_email_body(target, inc_u, inc_m)
    if not body: return "Team not found."
    if not subject.strip():
        subject = f"{kind} — {team['name']} — {_format_date(target)}"
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
        return f"Everyone already submitted for {_format_date(target)}."
    lines = [f"Reminder results — '{team['name']}' — {_format_date(target)}:"]
    if sent:   lines += [f"\n✅  Sent ({len(sent)}):"]   + [f"  •  {s}" for s in sent]
    if failed: lines += [f"\n❌  Failed ({len(failed)}):"] + [f"  •  {f}" for f in failed]
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
def send_user_updates_email(
    user_name: str,
    to_email: str,
    days: Union[int, str] = 1,
) -> str:
    """Send a specific member's updates to an email address. Leaders only."""
    err = _check_leader()
    if err: return err
    if not _user_in_own_team(user_name):
        return f"'{_format_name(user_name)}' is not in your team."
    days = max(int(days), 1)
    user = get_user_by_name(user_name)
    if not user: return f"No user found: '{user_name}'."
    updates = get_updates_by_user_and_days(user["id"], days)
    if not updates:
        return f"No updates found for {_format_name(user['name'])} in the last {days} day(s)."
    lines = [f"Hi,\n\nHere are the updates from {_format_name(user['name'])}:\n"]
    for u in updates:
        lines += [f"Date: {_format_date(u['date'])}", _strip_html(u["content"]), ""]
    lines.append("Thanks,\nTeam Tracker")
    body = "\n".join(lines).strip()
    subj = f"{_format_name(user['name'])}'s Update — {_format_date(updates[0]['date'])}"
    ok, msg = send_email(to_email, subj, body, [])
    return (
        f"✅  {_format_name(user['name'])}'s update sent to {to_email}."
        if ok else f"❌  Failed: {msg}"
    )


TOOLS = [
    get_user_updates, get_team_updates, get_missing_updates,
    get_meeting_notes_tool, get_team_members_info, summarize_updates,
    send_email_report, send_missing_update_reminders,
    get_standup_digest, send_user_updates_email,
]
_TOOL_MAP = {t.name: t for t in TOOLS}


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

_SYS = """You are Team Tracker — a smart assistant for team leaders.
Today: {today}
User : {profile}

RULES
1. Only answer team-related questions.
2. Use EXACTLY ONE tool per query.
3. Only leaders/managers may call tools.
4. If ambiguous, ask ONE short clarifying question.
5. Never expose raw errors — return a friendly message.
6. Support Hinglish queries naturally.
7. Confirm after every send/email action.
8. Keep replies concise — no bullet lists of commands.
9. If no data found, reply in one short line only.
10. Never list capabilities unless user types 'help'.

TOOL SELECTION
- show/give [member] update          → get_user_updates
- show all / sabka update            → get_team_updates
- who missing / pending              → get_missing_updates
- meeting notes / MoM                → get_meeting_notes_tool
- list members / who is in team      → get_team_members_info
- summary / full report              → summarize_updates
- send report to [email]             → send_email_report
- remind missing                     → send_missing_update_reminders
- standup digest / snapshot          → get_standup_digest
- send [member] update to [person]   → send_user_updates_email

EXAMPLES
"show ankur update"            → get_user_updates(user_name="Ankur", days=1)
"last 3 days ankur update"     → get_user_updates(user_name="Ankur", days=3)
"send ankur update to tarun"   → send_user_updates_email(user_name="Ankur", to_email=<tarun email>)
"who didn't update today"      → get_missing_updates()
"remind missing"               → send_missing_update_reminders()
"standup digest"               → get_standup_digest()
"how to cook pasta?"           → "I can only help with team-related questions."

TONE
- Concise and friendly.
- ✅ success  ❌ error  ⏳ pending.
- Match user language (Hinglish if they write Hinglish).
- Never suggest commands or list features in responses.
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
             "kon nahi","kaun nahi"]
_SUM_P    = ["summary","summarize","summarise","all updates","full report","team report",
             "team summary","team update","all update","sab updates","sari updates","poori summary"]
_TODAY_P  = ["updates today","today's update","todays update","team update today",
             "all member update","all members update","give me all member","sabka update",
             "sab ka update","all team update","everyone's update","everyone update",
             "sare members","poore team"]
_MOM_P    = ["meeting note","meeting-note","m.o.m","minutes of meeting",
             "meeting minute","mins of meeting","team notes","daily notes"]
_TEAM_KW  = ["update","submit","missing","pending","remind","digest","standup","meeting","mom",
             "note","member","team","email","send","mail","report","summary","leader","manager",
             "who","list","show","give","tell","bhej","kon","kaun"]


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
    asks_today = _has(text, _TODAY_P)
    asks_remind= _has(text, _REMIND_P)
    asks_digest= _has(text, _DIGEST_P)
    asks_names = _has(text, _NAMES_P)
    asks_my    = _has(text, _MY_P)

    raw_email = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", user_input)
    recip = raw_email.group(0) if raw_email else None
    if has_send and not recip:
        if re.search(r"\b(manager|boss)\b", text):
            mgrs = get_managers()
            if mgrs: recip = mgrs[0]["email"]
        if not recip:
            recip = _email_after_keyword(text) or _email_from_text(text)

    # ── follow-up "send this to X" ─────────────────────────────────────────────
    if has_send and recip and _last_context and (_has_ref(text) or not has_update):
        if _last_user_sent:
            _last_recipient = recip
            return send_user_updates_email.invoke(
                {"user_name": _last_user_sent, "to_email": recip}
            )
        ct = {"mom": "mom", "summary": "both"}.get(_last_context, "updates")
        _last_recipient = recip
        return send_email_report.invoke({"to_email": recip, "content_type": ct})

    # ── meeting notes ──────────────────────────────────────────────────────────
    if asks_mom:
        if has_send and recip:
            _last_context = "mom"; _last_user_sent = None; _last_recipient = recip
            return send_email_report.invoke({"to_email": recip, "content_type": "mom"})
        _last_context = "mom"; _last_user_sent = None
        return get_meeting_notes_tool.invoke({})

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

    # ── send summary ───────────────────────────────────────────────────────────
    if has_send and asks_sum and recip:
        _last_context = "summary"; _last_user_sent = None; _last_recipient = recip
        return send_email_report.invoke({"to_email": recip, "content_type": "both"})

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
    if asks_today and not has_send:
        _last_context = "updates"; _last_user_sent = None
        return get_team_updates.invoke({})

    # ── show one member's update ───────────────────────────────────────────────
    if has_update and not has_send and not asks_sum and not asks_miss:
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
            if subject_mem:
                _last_context = "updates"
                _last_recipient = final_recip
                _last_user_sent = subject_mem["name"]
                return send_user_updates_email.invoke(
                    {"user_name": subject_mem["name"], "to_email": final_recip}
                )
            single = _find_member_in_text(text, members)
            if single and single["email"] != final_recip:
                _last_context = "updates"
                _last_recipient = final_recip
                _last_user_sent = single["name"]
                return send_user_updates_email.invoke(
                    {"user_name": single["name"], "to_email": final_recip}
                )
            return "Whose update do you want to send, and to whom?"

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