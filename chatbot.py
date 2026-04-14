"""
chatbot.py – LangChain agent for Team Daily Update Tracker.
Uses Groq's llama-3.3-70b-versatile model with tool-calling to answer
queries about team updates, missing submissions, meeting notes, and
to send formatted email reports.
"""
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
    get_user_by_name,
    get_updates_by_user_and_days,
    get_all_teams_updates_by_date,
    get_missing_users_today,
    get_all_teams,
    get_team_by_id,
    get_users_by_team,
    get_team_members_emails,
    get_meeting_notes as db_get_meeting_notes,
)

_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH, override=True)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------
def _strip_html(html_content: str) -> str:
    """Convert HTML to readable plain text."""
    if not html_content:
        return ""
    text = html_content
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'<p[^>]*>', '', text)
    text = re.sub(r'<li[^>]*>', '• ', text)
    text = re.sub(r'</li>', '\n', text)
    text = re.sub(r'</?[uo]l[^>]*>', '\n', text)
    text = re.sub(r'</h[1-6]>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_module.unescape(text)
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = '\n'.join(line.rstrip() for line in text.splitlines())
    return text.strip()


# ---------------------------------------------------------------------------
# Tool 1: get_user_updates
# ---------------------------------------------------------------------------
@tool
def get_user_updates(user_name: str, days: Union[int, str] = 1) -> str:
    """Get the last N days of updates for a user identified by name.
    Returns a formatted string with dates and plain-text update content.
    Use this when asked about a specific person's updates.
    Default days=1 (today only) unless the user explicitly asks for more days, yesterday, or a date range.
    IMPORTANT: Return the full content to the user exactly as-is. Do not summarize or shorten it."""
    days = int(days)
    user = get_user_by_name(user_name)
    if user is None:
        return f"No user found with name matching '{user_name}'."
    updates = get_updates_by_user_and_days(user["id"], days)
    if not updates:
        return f"No updates found for {user['name']} in the last {days} day(s)."
    lines = [f"Updates for {user['name']} (last {days} day(s)):\n"]
    for upd in updates:
        lines.append(f"Date: {upd['date']}")
        lines.append(_strip_html(upd["content"]))
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Tool 2: get_team_updates
# ---------------------------------------------------------------------------
@tool
def get_team_updates(date: Optional[str] = None) -> str:
    """Get all team updates for a given date (YYYY-MM-DD).
    Omit the date parameter entirely to get today's updates.
    Returns formatted text grouped by team and member.
    IMPORTANT: Display the full content to the user exactly as-is. Do not summarize or shorten it."""
    target_date = date if date else str(dt_date.today())
    rows = get_all_teams_updates_by_date(target_date)
    if not rows:
        return f"No updates found for {target_date}."
    teams: dict[str, list] = {}
    for row in rows:
        team_name = row["team_name"]
        teams.setdefault(team_name, []).append(row)
    lines = [f"Team updates for {target_date}:\n"]
    for team_name, members in teams.items():
        lines.append(f"=== Team: {team_name} ===")
        for row in members:
            lines.append(f"\n{row['user_name']} ({row['user_role']}):")
            lines.append(_strip_html(row["content"]))
            lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Tool 3: get_missing_updates
# ---------------------------------------------------------------------------
@tool
def get_missing_updates(date: Optional[str] = None) -> str:
    """Get list of team members who have NOT submitted an update for a given date.
    Omit the date parameter entirely for today. Returns names and emails grouped by team."""
    target_date = date if date else str(dt_date.today())
    teams = get_all_teams()
    lines = [f"Missing updates for {target_date}:\n"]
    any_missing = False
    for team in teams:
        missing = get_missing_users_today(team["id"], target_date)
        if missing:
            any_missing = True
            lines.append(f"Team {team['name']}:")
            for user in missing:
                lines.append(f"  - {user['user_name']} ({user['email']})")
            lines.append("")
    if not any_missing:
        return f"All team members have submitted updates for {target_date}."
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Tool 4: get_meeting_notes_tool
# ---------------------------------------------------------------------------
@tool
def get_meeting_notes_tool(team_name: str, date: Optional[str] = None) -> str:
    """Get the meeting MoM/notes for a team on a given date.
    Omit the date parameter entirely for today's notes."""
    target_date = date if date else str(dt_date.today())
    teams = get_all_teams()
    matched_team = None
    for team in teams:
        if team["name"].lower() == team_name.strip().lower():
            matched_team = team
            break
    if matched_team is None:
        return f"No team found with name '{team_name}'. Available teams: {', '.join(t['name'] for t in teams)}."
    notes = db_get_meeting_notes(matched_team["id"], target_date)
    if notes is None:
        return f"No meeting notes found for team '{matched_team['name']}' on {target_date}."
    return (
        f"Meeting notes for team '{matched_team['name']}' on {target_date}:\n\n"
        + _strip_html(notes["content"])
    )


# ---------------------------------------------------------------------------
# Tool 5: get_team_members_info
# ---------------------------------------------------------------------------
@tool
def get_team_members_info(team_name: str) -> str:
    """Get the list of members (name, email, role) for a team.
    Use when asked about who is in a team, team members, teammates, etc."""
    teams = get_all_teams()
    matched_team = None
    for team in teams:
        if team["name"].lower() == team_name.strip().lower():
            matched_team = team
            break
    if matched_team is None:
        return f"No team found with name '{team_name}'."
    members = get_users_by_team(matched_team["id"])
    if not members:
        return f"No members found in team '{matched_team['name']}'."
    lines = [f"Members of team '{matched_team['name']}':"]
    for m in members:
        lines.append(f"  - {m['name']} ({m['role']}) — {m['email']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helper: _build_email_body  (NOT a tool — plain function)
# ---------------------------------------------------------------------------
def _build_email_body(
    team_name: str,
    target_date: str,
    include_updates: bool = True,
    include_mom: bool = True,
) -> str | None:
    """Internal helper — builds a clean plain-text email body for the given team and date.
    Returns None if the team is not found.

    include_updates: if True, member updates are included in the body.
    include_mom: if True, meeting notes (MoM) are included in the body.
    At least one of them must be True.
    """
    if not include_updates and not include_mom:
        return None

    all_teams = get_all_teams()
    include_all = (not team_name) or team_name.strip().lower() == "all"
    if include_all:
        selected_teams = list(all_teams)
    else:
        selected_teams = [
            t for t in all_teams
            if t["name"].lower() == team_name.strip().lower()
        ]
    if not selected_teams:
        return None

    all_updates = get_all_teams_updates_by_date(target_date) if include_updates else []
    updates_by_team: dict[int, list] = {}
    for row in all_updates:
        updates_by_team.setdefault(row["team_id"], []).append(row)

    sections: list[str] = []
    for team in selected_teams:
        parts: list[str] = []

        if include_updates:
            team_updates = updates_by_team.get(team["id"], [])
            member_lines: list[str] = []
            for row in team_updates:
                member_lines.append(row["user_name"])
                member_lines.append(_strip_html(row["content"]))
                member_lines.append("")
            if member_lines:
                parts.append("\n".join(member_lines).rstrip())
            else:
                parts.append("(No updates submitted.)")

            missing = get_missing_users_today(team["id"], target_date)
            if missing:
                missing_names = ", ".join(u["user_name"] for u in missing)
                parts.append(
                    f"Note: {missing_names} did not submit an update on {target_date}. "
                    "They have been CC'd on this email."
                )

        if include_mom:
            notes_row = db_get_meeting_notes(team["id"], target_date)
            if notes_row:
                mom_text = "MoM / Meeting Notes:\n" + _strip_html(notes_row["content"])
                parts.append(mom_text)
            else:
                parts.append(f"No meeting notes recorded for {target_date}.")

        header = f"=== Team {team['name']} ===\n\n" if include_all and len(selected_teams) > 1 else ""
        sections.append(header + "\n\n".join(parts))

    if include_updates and include_mom:
        intro = "Please find below the daily updates and meeting notes from the team."
    elif include_updates:
        intro = "Please find below the daily updates from the team."
    else:
        intro = "Please find below the meeting notes from the team."

    body = (
        "Hi Team,\n\n"
        f"{intro}\n\n"
        + "\n\n".join(sections)
    )
    return body.strip()


# ---------------------------------------------------------------------------
# Tool 6: summarize_updates
# ---------------------------------------------------------------------------
@tool
def summarize_updates(team_name: str, date: Optional[str] = None) -> str:
    """Show the team updates for a given date as clean plain text (for display in chat).
    Includes MoM if meeting notes exist. Omit the date parameter entirely for today.
    IMPORTANT: Display the full returned text exactly as-is. Do not shorten or reformat it."""
    target_date = date if date else str(dt_date.today())
    all_teams = get_all_teams()
    body = _build_email_body(team_name, target_date, include_updates=True, include_mom=True)
    if body is None:
        return (
            f"No team found with name '{team_name}'. "
            f"Available teams: {', '.join(t['name'] for t in all_teams)}."
        )
    return body


# ---------------------------------------------------------------------------
# Tool 7: send_email_report
# ---------------------------------------------------------------------------
@tool
def send_email_report(
    to_email: str,
    subject: str,
    team_name: str,
    date: Optional[str] = None,
    content_type: str = "updates",
) -> str:
    """Send a team email report.

    content_type controls WHAT is sent — this is important:
      - "updates"  : send ONLY member daily updates (no MoM). Use when the user asks
                     to send "updates", "daily updates", "standup", "team updates".
      - "mom"      : send ONLY meeting notes / MoM (no member updates). Use when the
                     user asks to send "meeting notes", "MoM", "minutes of meeting".
      - "both"     : send updates AND MoM together. Use ONLY when the user explicitly
                     asks for both, e.g. "send updates and MoM".

    Default is "updates". NEVER default to "both".

    Builds the email body internally — do NOT pass a body parameter.
    Automatically CC's all team members (even those who didn't submit).
    team_name: team to send for, or 'all' for all teams.
    Omit the date parameter entirely for today.
    """
    target_date = date if date else str(dt_date.today())
    all_teams = get_all_teams()

    ct = (content_type or "updates").strip().lower()
    if ct in ("update", "updates", "daily", "standup"):
        include_updates, include_mom = True, False
        default_subject_kind = "Daily Updates"
    elif ct in ("mom", "meeting_notes", "meeting-notes", "notes", "minutes"):
        include_updates, include_mom = False, True
        default_subject_kind = "Meeting Notes"
    elif ct in ("both", "all", "full"):
        include_updates, include_mom = True, True
        default_subject_kind = "Daily Updates & Meeting Notes"
    else:
        include_updates, include_mom = True, False
        default_subject_kind = "Daily Updates"

    body = _build_email_body(
        team_name,
        target_date,
        include_updates=include_updates,
        include_mom=include_mom,
    )
    if body is None:
        return (
            f"No team found with name '{team_name}'. "
            f"Available teams: {', '.join(t['name'] for t in all_teams)}."
        )

    # If user didn't give a subject, fall back to a sensible one
    if not subject or not subject.strip():
        subject = f"{default_subject_kind} — {target_date}"

    include_all = (not team_name) or team_name.strip().lower() == "all"
    cc_emails: list[str] = []
    if include_all:
        for team in all_teams:
            cc_emails.extend(get_team_members_emails(team["id"]))
    else:
        matched = [t for t in all_teams if t["name"].lower() == team_name.strip().lower()]
        cc_emails = get_team_members_emails(matched[0]["id"])
    seen: set[str] = set()
    unique_cc: list[str] = []
    for email in cc_emails:
        if email not in seen and email.lower() != to_email.lower():
            seen.add(email)
            unique_cc.append(email)
    success, message = send_email(to_email, subject, body, unique_cc)
    if success:
        cc_count = len(unique_cc)
        return (
            f"Email sent successfully to {to_email} ({default_subject_kind})"
            + (f" with {cc_count} CC recipient(s)." if cc_count else ".")
        )
    return f"Failed to send email: {message}"


# ---------------------------------------------------------------------------
# Tools list
# ---------------------------------------------------------------------------
TOOLS = [
    get_user_updates,
    get_team_updates,
    get_missing_updates,
    get_meeting_notes_tool,
    get_team_members_info,
    summarize_updates,
    send_email_report,
]


# ---------------------------------------------------------------------------
# LLM (cached — created once per process)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_llm():
    import os
    api_key = os.getenv("GROQ_API_KEY") or dotenv_values(_ENV_PATH).get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not found")
    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0,
        groq_api_key=api_key,
    )
    return llm.bind_tools(TOOLS, tool_choice="auto")


_TOOL_MAP = {t.name: t for t in TOOLS}

_SYSTEM_PROMPT = """You are a friendly team update assistant. Today: {today}.
RULES:
- Never mention tool names, function names, or technical implementation details to the user.
- Only provide information about the current user's own team. Refuse requests about other teams.
- Use tools to fetch all data — never make up information.
- When asked about team members, use the user's team name to look them up.
- ALWAYS display the full data returned by tools directly in your response. Never say "I've got the list" or similar — show the actual content.
- NEVER summarize, shorten, paraphrase, or rewrite update content submitted by team members. Show it exactly word-for-word as returned by the tool.

EMAIL CONTENT RULES (VERY IMPORTANT — read carefully):
- When calling send_email_report, you MUST pick the correct content_type based on what the user literally asked for:
    * User says "send updates", "send daily updates", "send standup", "send team updates"
        -> content_type="updates"   (member updates ONLY, NO meeting notes)
    * User says "send meeting notes", "send MoM", "send minutes of meeting", "send notes"
        -> content_type="mom"       (meeting notes ONLY, NO member updates)
    * User EXPLICITLY says "send updates AND meeting notes" / "send updates AND MoM" / "send everything" / "send full report"
        -> content_type="both"
- NEVER default to "both". If the request is ambiguous, default to "updates".
- NEVER mix member updates into a meeting-notes email, and NEVER mix meeting notes into an updates email, unless the user explicitly asked for both.
- For email reports, call send_email_report directly with to_email, subject, team_name, date, and content_type. Do NOT pass a body — it builds the body internally. Never call summarize_updates before send_email_report.

- DATE RULE: Always default to today's date unless the user explicitly says "yesterday", "day before yesterday", a specific date, or asks for multiple days (e.g. "today and yesterday"). Never guess a past date. OMIT the date parameter entirely when you want today — do NOT pass an empty string.
- FUTURE DATE RULE: If the user asks for updates or wants to send an email for tomorrow or any future date, politely refuse. Updates for future dates do not exist. Do not call any tool in that case.
- Answer naturally as an assistant, not as a developer.
Current user profile:
{user_profile}"""


# ---------------------------------------------------------------------------
# Public API (called from Streamlit)
# ---------------------------------------------------------------------------
def run_chatbot_query(user_input: str, chat_history: list, user_info: dict | None = None) -> str:
    """Run a tool-calling loop using bind_tools (compatible with all LangChain versions).
    chat_history is a list of BaseMessage objects.
    user_info: dict with keys name, email, role, team_id, team_name (optional).
    Returns the final string response."""
    llm_with_tools = _get_llm()
    if user_info:
        profile_lines = [
            f"Name: {user_info.get('name', 'Unknown')}",
            f"Email: {user_info.get('email', 'Unknown')}",
            f"Role: {user_info.get('role', 'Unknown')}",
        ]
        if user_info.get("team_name"):
            profile_lines.append(f"Team: {user_info['team_name']}")
        elif user_info.get("team_id"):
            profile_lines.append(f"Team ID: {user_info['team_id']}")
        user_profile = "\n".join(profile_lines)
    else:
        user_profile = "Not available."
    system = SystemMessage(content=_SYSTEM_PROMPT.format(
        today=str(dt_date.today()),
        user_profile=user_profile,
    ))
    messages = [system] + list(chat_history) + [HumanMessage(content=user_input)]
    last_tool_results: list[str] = []
    for _ in range(5):
        response = llm_with_tools.invoke(messages)
        messages.append(response)
        if not getattr(response, "tool_calls", None):
            break
        last_tool_results = []
        for tc in response.tool_calls:
            tool_fn = _TOOL_MAP.get(tc["name"])
            if tool_fn:
                # Coerce string-typed ints to int (small models sometimes pass wrong types)
                schema = tool_fn.args_schema.model_fields if hasattr(tool_fn, "args_schema") and tool_fn.args_schema else {}
                coerced_args = {}
                for k, v in tc["args"].items():
                    field = schema.get(k)
                    if field and hasattr(field, "annotation") and field.annotation is int and isinstance(v, str):
                        try:
                            v = int(v)
                        except ValueError:
                            pass
                    # Drop empty-string date args so Optional defaults kick in
                    if k == "date" and v == "":
                        continue
                    coerced_args[k] = v
                result = tool_fn.invoke(coerced_args)
            else:
                result = f"Tool '{tc['name']}' not found."
            last_tool_results.append(str(result))
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
    final = response.content or ""
    if last_tool_results and len(final.strip()) < 80:
        final = "\n\n".join(last_tool_results)
    return final