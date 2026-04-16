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
        plain_part = MIMEText(body, "plain", "utf-8")

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


# ── HTML builder ───────────────────────────────────────────────────────────────

def _build_html(subject: str, body: str, sender: str) -> str:
    """Return a responsive, email-client-safe HTML string."""

    # Convert plain newlines to <br> so paragraph breaks survive HTML rendering
    html_body_content = "".join(
        f"<p>{line}</p>" if line.strip() else "<br>"
        for line in body.splitlines()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">

  <!-- Outer wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:#f4f4f7;padding:32px 0;">
    <tr>
      <td align="center">

        <!-- Card -->
        <table width="600" cellpadding="0" cellspacing="0" border="0"
               style="max-width:600px;width:100%;background-color:#ffffff;
                      border-radius:8px;overflow:hidden;
                      box-shadow:0 2px 8px rgba(0,0,0,.08);">

          <!-- Header -->
          <tr>
            <td style="background-color:#4f46e5;padding:28px 40px;">
              <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:700;
                         letter-spacing:.3px;">{subject}</h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;color:#374151;font-size:15px;
                       line-height:1.7;">
              {html_body_content}
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:0 40px;">
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:0;" />
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 40px;color:#9ca3af;font-size:12px;
                       text-align:center;">
              Sent by <strong style="color:#6b7280;">{sender}</strong>
              &nbsp;·&nbsp; Please do not reply directly to this email.
            </td>
          </tr>

        </table>
        <!-- /Card -->

      </td>
    </tr>
  </table>
  <!-- /Outer wrapper -->

</body>
</html>"""