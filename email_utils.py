import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()


def send_email(
    to_email: str,
    subject: str,
    body: str,
    cc_emails: list[str] | None = None,
) -> tuple[bool, str]:
    """Send HTML email via SMTP_HOST:SMTP_PORT with STARTTLS.

    Reads from env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
    Builds MIMEMultipart('alternative') message with plain-text fallback + HTML.
    CC header = comma-joined cc_emails.
    RCPT TO includes both to_email and all cc_emails.
    Returns (True, 'Email sent successfully.') or (False, error_message_string).
    Catches all exceptions and returns (False, str(e)) — never raises.
    """
    cc_emails = cc_emails or []

    try:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port_str = os.environ.get("SMTP_PORT")
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASS")
        smtp_from = os.environ.get("SMTP_FROM")

        missing = [
            k for k, v in {
                "SMTP_HOST": smtp_host,
                "SMTP_PORT": smtp_port_str,
                "SMTP_USER": smtp_user,
                "SMTP_PASS": smtp_pass,
                "SMTP_FROM": smtp_from,
            }.items()
            if not v
        ]

        if missing:
            return (False, f"Missing SMTP config in .env: {', '.join(missing)}")

        smtp_port = int(smtp_port_str)

        # ── Build message ──────────────────────────────────────────────────────
        msg = MIMEMultipart("alternative")  # allows plain + HTML parts
        msg["From"] = smtp_from
        msg["To"] = to_email
        msg["Subject"] = subject
        if cc_emails:
            msg["CC"] = ", ".join(cc_emails)

        # Plain-text fallback (for clients that don't render HTML)
        plain_part = MIMEText(_to_plain(body), "plain", "utf-8")

        # HTML part — simple, clean, widely-compatible structure
        html_body = _build_html(subject, body, smtp_from)
        html_part = MIMEText(html_body, "html", "utf-8")

        # Attach plain first, HTML last — clients prefer the last part they support
        msg.attach(plain_part)
        msg.attach(html_part)

        all_recipients = [to_email] + cc_emails

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, all_recipients, msg.as_string())

        return (True, "Email sent successfully.")

    except Exception as e:
        return (False, str(e))


# ── helpers ────────────────────────────────────────────────────────────────────

def _to_plain(body: str) -> str:
    """Strip html-body wrapper and all tags to produce a plain-text fallback."""
    import re as _re, html as _html
    text = _re.sub(r"<html-body>|</html-body>", "", body)
    text = _re.sub(r"<br\s*/?>", "\n", text)
    text = _re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = _re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text).replace("\xa0", " ")
    return _re.sub(r"\n{3,}", "\n\n", text).strip()


def _build_html(subject: str, body: str, sender: str) -> str:
    """Return a responsive, email-client-safe HTML string.

    If body is wrapped in <html-body>…</html-body> (structured HTML from
    _build_email_body), it is embedded directly.  Otherwise plain text is
    converted to <p> tags.
    """
    import re as _re

    if "<html-body>" in body:
        # Extract pre-built HTML content
        m = _re.search(r"<html-body>(.*?)</html-body>", body, _re.DOTALL)
        html_body_content = m.group(1).strip() if m else body
    else:
        # Plain-text fallback → simple paragraph conversion
        html_body_content = "".join(
            f"<p style='margin:0 0 8px;'>{line}</p>" if line.strip() else "<br>"
            for line in body.splitlines()
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#f0f2f5;font-family:Arial,Helvetica,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:#f0f2f5;padding:32px 0;">
    <tr>
      <td align="center">

        <table width="620" cellpadding="0" cellspacing="0" border="0"
               style="max-width:620px;width:100%;background-color:#ffffff;
                      border-radius:10px;overflow:hidden;
                      box-shadow:0 4px 16px rgba(0,0,0,.10);">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);
                       padding:28px 36px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td>
                    <div style="color:#c7d2fe;font-size:11px;font-weight:600;
                                letter-spacing:1px;text-transform:uppercase;
                                margin-bottom:4px;">Team Update Tracker</div>
                    <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:700;
                               line-height:1.3;">{subject}</h1>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px 36px;color:#374151;font-size:14px;line-height:1.7;">
              {html_body_content}
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:0 36px;">
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:0;" />
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:18px 36px;color:#9ca3af;font-size:12px;text-align:center;">
              Sent by <strong style="color:#6b7280;">{sender}</strong>
              &nbsp;·&nbsp; Please do not reply directly to this email.
            </td>
          </tr>

        </table>

      </td>
    </tr>
  </table>

</body>
</html>"""