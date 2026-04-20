"""
scheduler.py — Standalone scheduled email sender.

Run with: python scheduler.py
Polls every 60 seconds. When a schedule's send_time matches current HH:MM
and the current day matches the schedule's days setting, sends the email.
"""

import json
import logging
import re
import time
from datetime import date, datetime
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

        if rows:
            update_cards = ""
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

    kind = {
        "updates": "Daily Updates",
        "mom": "Meeting Notes",
        "both": "Daily Updates & MoM",
    }.get(content_type, "Report")
    subject = f"{kind} — {_format_name(team['name'])} — {_format_date(target)}"

    manual_recipients = json.loads(schedule["recipients"] or "[]")
    team_emails = get_team_members_emails(team_id) if auto_cc_team else []

    if not manual_recipients and not team_emails:
        log.warning("Schedule '%s': no recipients configured, skipping.", label)
        return

    if manual_recipients:
        to_email = manual_recipients[0]
        cc = list(dict.fromkeys(
            manual_recipients[1:] + [e for e in team_emails if e.lower() != to_email.lower()]
        ))
    else:
        to_email = team_emails[0]
        cc = list(dict.fromkeys(team_emails[1:])) if len(team_emails) > 1 else []

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
