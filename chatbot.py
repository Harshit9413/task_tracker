import re
import html as html_module
from datetime import date as dt_date
from functools import lru_cache
from typing import Union, Optional
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
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


# ===========================================================================
# Helpers
# ===========================================================================
_current_user = None
_last_context = None  # "mom", "updates", "summary", "missing", "digest", "leader"


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        val = row[key]
        return default if val is None else val
    except (KeyError, IndexError):
        return default


def _is_leader(user) -> bool:
    if not user:
        return False
    role = (_row_get(user, "role") or "").strip().lower()
    return role in ("leader", "lead", "team_leader", "team-leader", "manager", "admin")


def _check_leader() -> Optional[str]:
    if not _current_user:
        return "Access denied: user information not available."
    if not _is_leader(_current_user):
        return ("Access denied: only team leaders can view or send team data. "
                "Please contact your team leader.")
    return None


def _own_team_name() -> Optional[str]:
    return _row_get(_current_user, "team_name")


def _own_team_id() -> Optional[int]:
    return _row_get(_current_user, "team_id")


def _strip_html(html_content: str) -> str:
    if not html_content:
        return ""
    text = html_content
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'<li[^>]*>', '• ', text)
    text = re.sub(r'</li>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_module.unescape(text).replace('\xa0', ' ')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _find_team(team_name: str):
    for t in get_all_teams():
        if t["name"].lower() == team_name.strip().lower():
            return t
    return None


def _own_team():
    tid = _own_team_id()
    if tid is not None:
        for t in get_all_teams():
            if t["id"] == tid:
                return t
    name = _own_team_name()
    if name:
        return _find_team(name)
    return None


def _user_in_own_team(user_name: str) -> bool:
    team = _own_team()
    if not team:
        return False
    members = get_users_by_team(team["id"])
    return any(m["name"].lower() == user_name.lower() for m in members)


def _lookup_person_email(text: str) -> Optional[str]:
    """Look up an email by matching a person's name in text.
    Checks team members + org managers. Tries full name first, then first name.
    """
    team = _own_team()
    candidates = []
    if team:
        candidates.extend(get_users_by_team(team["id"]))
    candidates.extend(get_managers())

    text_l = text.lower()
    for p in candidates:
        full = (p["name"] or "").lower().strip()
        if full and full in text_l:
            return p["email"]
    for p in candidates:
        name = (p["name"] or "").strip()
        if not name:
            continue
        first = name.split()[0].lower()
        if re.search(rf"\b{re.escape(first)}\b", text_l):
            return p["email"]
    return None


# ===========================================================================
# Tools
# ===========================================================================
@tool
def get_user_updates(user_name: str, days: Union[int, str] = 1) -> str:
    """Get last N days of updates for a user from your team. Leaders only."""
    err = _check_leader()
    if err:
        return err
    if not _user_in_own_team(user_name):
        return f"Access denied: '{user_name}' is not in your team."
    days = int(days)
    user = get_user_by_name(user_name)
    if not user:
        return f"No user found: '{user_name}'."
    updates = get_updates_by_user_and_days(user["id"], days)
    if not updates:
        return f"No updates for {user['name']} in last {days} day(s)."
    lines = [f"Updates for {user['name']} (last {days} day(s)):\n"]
    for u in updates:
        lines.append(f"Date: {u['date']}")
        lines.append(_strip_html(u["content"]))
        lines.append("")
    return "\n".join(lines).strip()


@tool
def get_team_updates(date: Optional[str] = None) -> str:
    """Get all updates for YOUR team. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    target = date or str(dt_date.today())
    rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
    if not rows:
        return f"No updates for team '{team['name']}' on {target}."
    lines = [f"Updates for team '{team['name']}' on {target}:\n"]
    for r in rows:
        lines.append(f"\n{r['user_name']} ({r['user_role']}):")
        lines.append(_strip_html(r["content"]))
        lines.append("")
    return "\n".join(lines).strip()


@tool
def get_missing_updates(date: Optional[str] = None) -> str:
    """Get YOUR team members who did NOT submit an update. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    target = date or str(dt_date.today())
    missing = get_missing_users_today(team["id"], target)
    if not missing:
        return f"All members of '{team['name']}' submitted updates for {target}."
    lines = [f"Missing updates for team '{team['name']}' on {target}:"]
    for u in missing:
        lines.append(f"  - {_row_get(u, 'user_name', 'Unknown')} ({_row_get(u, 'email', 'no email')})")
    return "\n".join(lines)


@tool
def get_meeting_notes_tool(date: Optional[str] = None) -> str:
    """Get YOUR team's meeting notes. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    target = date or str(dt_date.today())
    notes = db_get_meeting_notes(team["id"], target)
    if not notes:
        return f"No meeting notes for '{team['name']}' on {target}."
    return f"Meeting notes for '{team['name']}' on {target}:\n\n{_strip_html(notes['content'])}"


@tool
def get_team_members_info() -> str:
    """List members of YOUR team. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    members = get_users_by_team(team["id"])
    if not members:
        return f"No members in '{team['name']}'."
    lines = [f"Members of '{team['name']}':"]
    for m in members:
        lines.append(f"  - {m['name']} ({m['role']}) — {m['email']}")
    return "\n".join(lines)


def _build_email_body(target: str, include_updates: bool = True, include_mom: bool = True):
    if not include_updates and not include_mom:
        return None
    team = _own_team()
    if not team:
        return None
    parts = []
    if include_updates:
        rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
        if rows:
            lines = []
            for r in rows:
                lines.append(r["user_name"])
                lines.append(_strip_html(r["content"]))
                lines.append("")
            parts.append("\n".join(lines).rstrip())
        else:
            parts.append("(No updates submitted.)")
        missing = get_missing_users_today(team["id"], target)
        if missing:
            names = ", ".join(_row_get(u, "user_name", "Unknown") for u in missing)
            parts.append(f"Note: {names} did not submit on {target}. They have been CC'd.")
    if include_mom:
        notes = db_get_meeting_notes(team["id"], target)
        if notes:
            parts.append("MoM / Meeting Notes:\n" + _strip_html(notes["content"]))
        else:
            parts.append(f"No meeting notes for {target}.")
    if include_updates and include_mom:
        intro = "Please find below the daily updates and meeting notes from the team."
    elif include_updates:
        intro = "Please find below the daily updates from the team."
    else:
        intro = "Please find below the meeting notes from the team."
    return f"Hi Team,\n\n{intro}\n\n" + "\n\n".join(parts)


@tool
def summarize_updates(date: Optional[str] = None) -> str:
    """Show YOUR team's updates + MoM. Leaders only."""
    err = _check_leader()
    if err:
        return err
    target = date or str(dt_date.today())
    body = _build_email_body(target, True, True)
    if body is None:
        return "Access denied: your team could not be identified."
    return body


@tool
def send_email_report(to_email: str, subject: str = "",
                      date: Optional[str] = None, content_type: str = "updates") -> str:
    """Send YOUR team's report email. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    target = date or str(dt_date.today())
    ct = (content_type or "updates").strip().lower()
    if ct in ("mom", "meeting_notes", "meeting-notes", "notes", "minutes"):
        inc_u, inc_m, kind = False, True, "Meeting Notes"
    elif ct in ("both", "all", "full", "summary"):
        inc_u, inc_m, kind = True, True, "Daily Updates & Meeting Notes"
    else:
        inc_u, inc_m, kind = True, False, "Daily Updates"
    body = _build_email_body(target, inc_u, inc_m)
    if body is None:
        return "Access denied: your team could not be identified."
    if not subject or not subject.strip():
        subject = f"{kind} — {team['name']} — {target}"
    cc = get_team_members_emails(team["id"])
    seen = set()
    unique_cc = []
    for e in cc:
        if e not in seen and e.lower() != to_email.lower():
            seen.add(e)
            unique_cc.append(e)
    ok, msg = send_email(to_email, subject, body, unique_cc)
    if ok:
        return f"Email sent to {to_email} ({kind})" + (f" with {len(unique_cc)} CC." if unique_cc else ".")
    return f"Failed to send email: {msg}"


@tool
def send_missing_update_reminders(date: Optional[str] = None,
                                  manager_email: Optional[str] = None) -> str:
    """Send reminder to YOUR team members who did NOT submit. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    target = date or str(dt_date.today())
    sent, failed = [], []
    for u in get_missing_users_today(team["id"], target):
        email = _row_get(u, "email")
        name = _row_get(u, "user_name", "there")
        if not email:
            failed.append(f"{name} (no email)")
            continue
        subject = f"Reminder: Please submit your daily update for {target}"
        body = (
            f"Hi {name},\n\n"
            f"This is a friendly reminder that your daily update for {target} "
            f"has not been submitted yet.\n\n"
            f"Please take a moment to share your update so the team stays in sync.\n\n"
            f"Thanks,\nTeam Update Tracker"
        )
        cc = [manager_email.strip()] if manager_email and manager_email.strip().lower() != email.lower() else []
        ok, msg = send_email(email, subject, body, cc)
        if ok:
            sent.append(f"{name} <{email}>")
        else:
            failed.append(f"{name} <{email}> ({msg})")
    if not sent and not failed:
        return f"No reminders needed — everyone in '{team['name']}' submitted their update for {target}."
    lines = [f"Reminder summary for team '{team['name']}' on {target}:"]
    if sent:
        lines.append(f"\nReminders sent ({len(sent)}):")
        lines += [f"  - {s}" for s in sent]
    if failed:
        lines.append(f"\nFailed ({len(failed)}):")
        lines += [f"  - {f}" for f in failed]
    if manager_email:
        lines.append(f"\nManager CC: {manager_email}")
    return "\n".join(lines)


@tool
def get_standup_digest(date: Optional[str] = None) -> str:
    """Standup snapshot for YOUR team. Leaders only."""
    err = _check_leader()
    if err:
        return err
    team = _own_team()
    if not team:
        return "Access denied: your team could not be identified."
    target = date or str(dt_date.today())
    rows = [r for r in get_all_teams_updates_by_date(target) if r["team_id"] == team["id"]]
    missing = get_missing_users_today(team["id"], target)
    notes = db_get_meeting_notes(team["id"], target)
    out = [f"Stand-up digest for '{team['name']}' on {target}\n"]
    out.append(f"Submitted: {len(rows)} | Pending: {len(missing)} | MoM: {'yes' if notes else 'no'}")
    if rows:
        out.append("\nSubmitted by:")
        for r in rows:
            preview = _strip_html(r["content"]).replace("\n", " ").strip()
            if len(preview) > 120:
                preview = preview[:117].rstrip() + "..."
            out.append(f"  - {r['user_name']}: {preview}")
    if missing:
        out.append("\nPending from:")
        for u in missing:
            out.append(f"  - {_row_get(u, 'user_name', 'Unknown')} <{_row_get(u, 'email', 'no email')}>")
    return "\n".join(out).strip()


@tool
def send_user_updates_email(user_name: str, to_email: str,
                            days: Union[int, str] = 1) -> str:
    """Send ONE team member's updates to a specific email address.
    Use when user says 'send Bob's updates to X'. Leaders only."""
    err = _check_leader()
    if err:
        return err
    if not _user_in_own_team(user_name):
        return f"Access denied: '{user_name}' is not in your team."
    days = int(days)
    user = get_user_by_name(user_name)
    if not user:
        return f"No user found: '{user_name}'."
    updates = get_updates_by_user_and_days(user["id"], days)
    if not updates:
        return f"No updates found for {user['name']}."
    body_lines = [f"Hi,\n\nBelow are the recent updates from {user['name']}:\n"]
    for u in updates:
        body_lines.append(f"Date: {u['date']}")
        body_lines.append(_strip_html(u["content"]))
        body_lines.append("")
    body_lines.append("Thanks,\nTeam Update Tracker")
    body = "\n".join(body_lines).strip()
    subject = f"{user['name']}'s update — {updates[0]['date']}"
    ok, msg = send_email(to_email, subject, body, [])
    if ok:
        return f"Email sent: {user['name']}'s updates have been sent to {to_email}."
    return f"Failed to send email: {msg}"


TOOLS = [
    get_user_updates, get_team_updates, get_missing_updates, get_meeting_notes_tool,
    get_team_members_info, summarize_updates, send_email_report,
    send_missing_update_reminders, get_standup_digest, send_user_updates_email,
]
_TOOL_MAP = {t.name: t for t in TOOLS}


@lru_cache(maxsize=1)
def _get_llm():
    import os
    api_key = os.getenv("GROQ_API_KEY") or dotenv_values(_ENV_PATH).get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not found")
    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0, groq_api_key=api_key)
    return llm.bind_tools(TOOLS, tool_choice="auto")


_SYSTEM_PROMPT = """You are a team update assistant. Today: {today}.
- Only leaders can use tools. Refuse non-leaders politely.
- All tools auto-scope to the leader's own team.
- Answer only what the user asked. Call exactly ONE tool per query.
- Never call multiple tools. Never mix results.

Current user: {user_profile}"""


# ===========================================================================
# Shortcut patterns
# ===========================================================================
_REMINDER_PATTERNS = [
    "remind missing", "ping pending", "ping missing", "remind pending",
    "send reminder", "reminder email", "reminder to those who",
    "send mail to those who", "mail those who didn", "mail to those who didn",
    "send mail to who", "send email to who", "send the mail who",
    "send the email who", "mail to who didn", "email to who didn",
    "send mail who doesn", "send mail who doest", "send email who doesn",
    "mail who didn", "email who didn", "mail who hasn", "email who hasn",
    "remind people who", "remind users who", "remind members who",
    "jisne update nahi", "jisne mail update nahi", "update nahi ki",
    "mail update ni ki", "mail update nahi ki", "update ni di",
    "update nahi di", "jisne nahi", "jinhone nahi",
    "use mail karo", "unko mail karo", "use reminder",
]

_DIGEST_PATTERNS = [
    "standup digest", "stand-up digest", "stand up digest",
    "today's snapshot", "todays snapshot", "quick summary of today",
    "who submitted today", "who is pending", "who's pending",
    "who updated", "who has updated", "who have updated",
    "list who updated", "list who has updated", "list who have updated",
    "who did update", "list of who updated", "list updated",
    "show who updated", "show me who updated", "give me list who updated",
    "members who updated", "submitted today", "submitted by",
    "list of submitted", "show submitted",
    "kisne update kiya", "kisne update di", "kin logon ne update",
]

_READ_ONLY_WORDS = ["list", "show", "give me", "tell me", "what is", "who is", "display", "view"]

_NOT_UPDATED_INDICATORS = [
    "not update", "didn't update", "didnt update", "hasn't update", "hasnt update",
    "doesn't update", "doesnt update", "haven't update", "have not update",
    "not submit", "didn't submit", "didnt submit", "hasn't submit",
    "pending", "missing", "nahi ki", "nahi di", "ni ki", "ni di",
]

# Keywords that indicate user wants FULL team summary / report
_SUMMARY_KEYWORDS = [
    "summary", "summarize", "summarise", "sumary", "sumery",
    "all updates", "all the updates", "all sumary", "all the sumary",
    "full report", "team report", "everything", "complete update",
    "team summary", "team's summary", "team's update", "teams update",
    "team update", "all update",
    "sab updates", "sari updates", "saari updates", "poori summary",
    "puri summary", "sari summary",
]

# Follow-up reference words: "this", "that", "it", "same", "above"
_REFERENCE_WORDS = [" this ", " this.", " this?", " this!", " that ", " that.",
                    " it ", " it.", " it?", " it!", " same ", " above ",
                    " yeh ", " ye ", " wahi ", " upar "]


def _matches_any(text: str, patterns: list) -> bool:
    return any(p in text for p in patterns)


def _has_reference_word(text: str) -> bool:
    padded = " " + text.strip() + " "
    return any(w in padded for w in _REFERENCE_WORDS)


def _try_shortcut(user_input: str) -> Optional[str]:
    """Bypass LLM for unambiguous requests. Tracks _last_context for follow-ups."""
    global _last_context
    text = user_input.lower()
    is_read_only = any(w in text for w in _READ_ONLY_WORDS)
    matches_digest = _matches_any(text, _DIGEST_PATTERNS)
    matches_reminder = _matches_any(text, _REMINDER_PATTERNS)
    asks_about_missing = any(p in text for p in _NOT_UPDATED_INDICATORS)
    asks_summary = _matches_any(text, _SUMMARY_KEYWORDS)
    email_match = re.search(r"[\w\.\-+]+@[\w\.\-]+\.\w+", user_input)

    send_words = ["send", "mail", "email", "forward", "share", "bhej"]
    has_send_kw = any(w in text for w in send_words)

    # Resolve recipient: explicit email > "manager" keyword > name lookup
    recipient_email = email_match.group(0) if email_match else None
    if has_send_kw and not recipient_email:
        if re.search(r"\b(manager|boss)\b", text):
            mgrs = get_managers()
            if mgrs:
                recipient_email = mgrs[0]["email"]
        if not recipient_email:
            recipient_email = _lookup_person_email(text)

    # ---------- Follow-up: "send this to <recipient>" ----------
    # Triggers when: send keyword + recipient + (reference word OR no content word)
    # AND previous context exists.
    has_content_word = any(kw in text for kw in [
        "meeting note", "mom", "minute", "update", "digest",
        "standup", "stand-up", "report", "reminder", "missing",
    ]) or asks_summary

    if has_send_kw and recipient_email and _last_context and (
        _has_reference_word(text) or not has_content_word
    ):
        if _last_context == "mom":
            return send_email_report.invoke({
                "to_email": recipient_email, "content_type": "mom",
            })
        if _last_context == "summary":
            return send_email_report.invoke({
                "to_email": recipient_email, "content_type": "both",
            })
        if _last_context in ("updates", "digest"):
            return send_email_report.invoke({
                "to_email": recipient_email, "content_type": "updates",
            })

    # ---------- Meeting notes / MoM ----------
    mom_keywords = ["meeting note", "meeting-note", "meetingnote",
                    "m.o.m", "minutes of meeting", "minutes of the meeting",
                    "meeting minute", "mins of meeting", "mins of the meeting",
                    "team notes", "daily notes"]
    asks_about_mom = any(kw in text for kw in mom_keywords)
    if re.search(r'\bmom\b', text):
        asks_about_mom = True
    if asks_about_mom and has_send_kw and recipient_email:
        _last_context = "mom"
        return send_email_report.invoke({
            "to_email": recipient_email, "content_type": "mom",
        })
    if asks_about_mom:
        result = get_meeting_notes_tool.invoke({})
        _last_context = "mom"
        return result

    # ---------- "Who is the leader / manager" (question, no send) ----------
    leader_keywords = ["leader", "team lead", "team-lead", "head", "admin"]
    manager_keywords = ["manager", "boss"]
    asks_question = any(w in text for w in [
        "who", "what", "tell", "show", "list", "give me", "kon", "kaun"
    ])
    asks_leader_q = any(kw in text for kw in leader_keywords)
    asks_manager_q = any(kw in text for kw in manager_keywords)

    if asks_question and (asks_leader_q or asks_manager_q) and not has_send_kw:
        team = _own_team()
        people = []
        if team and asks_leader_q:
            members = get_users_by_team(team["id"])
            for m in members:
                role = (m["role"] or "").lower()
                if role in ("leader", "lead", "team_leader", "team-leader", "admin"):
                    people.append(m)
        if asks_manager_q:
            people.extend(get_managers())

        seen = set()
        unique_people = []
        for p in people:
            if p["email"] not in seen:
                seen.add(p["email"])
                unique_people.append(p)

        _last_context = "leader"

        if not unique_people:
            label = "manager" if asks_manager_q and not asks_leader_q else "leader"
            team_label = f" for team '{team['name']}'" if team else ""
            return f"No {label} found{team_label}."

        if len(unique_people) == 1:
            m = unique_people[0]
            role_label = "Manager" if (m["role"] or "").lower() == "manager" else "Team leader"
            return f"{role_label}:\n  - {m['name']} ({m['role']}) — {m['email']}"

        if asks_manager_q and not asks_leader_q:
            header = "Managers:"
        elif asks_leader_q and not asks_manager_q:
            header = f"Team leaders of '{team['name']}':" if team else "Team leaders:"
        else:
            header = "Leaders / Managers:"

        lines = [header]
        for m in unique_people:
            lines.append(f"  - {m['name']} ({m['role']}) — {m['email']}")
        return "\n".join(lines)

    # ---------- Send SUMMARY / full report (MUST come before single-user forward) ----------
    if has_send_kw and asks_summary and recipient_email:
        _last_context = "summary"
        return send_email_report.invoke({
            "to_email": recipient_email, "content_type": "both",
        })

    # ---------- Forward ONE user's updates ----------
    has_update_kw = "update" in text
    if has_send_kw and has_update_kw and recipient_email and not is_read_only and not asks_about_missing and not asks_summary:
        team = _own_team()
        if team:
            members = get_users_by_team(team["id"])
            # Skip names that match the recipient (e.g., recipient is "tarun", don't treat "tarun" as subject)
            recipient_local = recipient_email.split("@")[0].lower()
            matched_member = None
            for m in members:
                first_name = m["name"].split()[0].lower()
                full_name = m["name"].lower()
                if first_name in recipient_local or full_name in recipient_local:
                    continue
                if re.search(rf"\b{re.escape(first_name)}\b", text):
                    matched_member = m
                    break
            if matched_member:
                _last_context = "updates"
                return send_user_updates_email.invoke({
                    "user_name": matched_member["name"],
                    "to_email": recipient_email,
                })
            _last_context = "updates"
            return send_email_report.invoke({
                "to_email": recipient_email, "content_type": "updates",
            })

    # ---------- Read-only "who NOT updated" -> missing list ----------
    if is_read_only and asks_about_missing:
        _last_context = "missing"
        return get_missing_updates.invoke({})

    # ---------- Read-only "who updated" -> digest ----------
    if is_read_only and (matches_digest or matches_reminder):
        _last_context = "digest"
        return get_standup_digest.invoke({})

    # ---------- Pure reminder ----------
    if matches_reminder:
        m = re.search(r"[\w\.\-+]+@[\w\.\-]+\.\w+", user_input)
        manager_email = m.group(0) if m else None
        _last_context = "reminder"
        return send_missing_update_reminders.invoke({"manager_email": manager_email})

    # ---------- Pure digest ----------
    if matches_digest:
        _last_context = "digest"
        return get_standup_digest.invoke({})

    # ---------- Show summary (no send) ----------
    if asks_summary and not has_send_kw:
        _last_context = "summary"
        return summarize_updates.invoke({})

    return None


# ===========================================================================
# Main entry point
# ===========================================================================
def run_chatbot_query(user_input: str, chat_history: list, user_info=None) -> str:
    """Run the chatbot. Returns a single response string."""
    global _current_user
    _current_user = user_info

    try:
        if not _is_leader(user_info):
            return ("Access denied: only team leaders can use this assistant. "
                    "Please contact your team leader.")

        shortcut = _try_shortcut(user_input)
        if shortcut is not None:
            return shortcut

        llm = _get_llm()
        lines = [
            f"Name: {_row_get(user_info, 'name', 'Unknown')}",
            f"Email: {_row_get(user_info, 'email', 'Unknown')}",
            f"Role: {_row_get(user_info, 'role', 'Unknown')} (LEADER)",
        ]
        if _row_get(user_info, "team_name"):
            lines.append(f"Team: {_row_get(user_info, 'team_name')}")
        profile = "\n".join(lines)

        system = SystemMessage(content=_SYSTEM_PROMPT.format(
            today=str(dt_date.today()), user_profile=profile))

        trimmed = list(chat_history)[-6:]
        safe_history = []
        for msg in trimmed:
            c = getattr(msg, "content", "")
            if isinstance(c, str) and len(c) > 2000:
                safe_history.append(msg.__class__(content=c[:2000] + "... [truncated]"))
            else:
                safe_history.append(msg)

        messages = [system] + safe_history + [HumanMessage(content=user_input)]
        all_results = []

        try:
            response = llm.invoke(messages)
            messages.append(response)

            if getattr(response, "tool_calls", None):
                tc = response.tool_calls[0]
                fn = _TOOL_MAP.get(tc["name"])
                if fn:
                    schema = fn.args_schema.model_fields if hasattr(fn, "args_schema") and fn.args_schema else {}
                    args = {}
                    for k, v in tc["args"].items():
                        f = schema.get(k)
                        if f and hasattr(f, "annotation") and f.annotation is int and isinstance(v, str):
                            try:
                                v = int(v)
                            except ValueError:
                                pass
                        if k == "date" and v == "":
                            continue
                        args[k] = v
                    try:
                        result = fn.invoke(args)
                    except Exception as e:
                        result = f"Tool error: {e}"
                    all_results.append(str(result))
        except Exception as e:
            err = str(e)
            if "rate_limit" in err.lower() or "429" in err:
                m = re.search(r"try again in ([\w\.]+)", err)
                hint = f" Please try again in {m.group(1)}." if m else ""
                if all_results:
                    return "⚠️ Token limit reached." + hint + "\n\n" + all_results[0]
                return "⚠️ Daily token limit reached." + hint
            if all_results:
                return all_results[0]
            return f"⚠️ Error: {err}"

        if all_results:
            return all_results[0]
        return (getattr(response, "content", "") or
                "I couldn't find a matching action for your request. "
                "Try: 'show who updated', 'show who didn't update', 'remind missing users', "
                "or 'send team's updates to <email>'.")
    finally:
        _current_user = None