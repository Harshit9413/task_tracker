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
    """Send plain-text email via SMTP_HOST:SMTP_PORT with STARTTLS.

    Reads from env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM

    Builds MIMEMultipart message with To, CC, Subject headers.
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

        missing = [k for k, v in {
            "SMTP_HOST": smtp_host, "SMTP_PORT": smtp_port_str,
            "SMTP_USER": smtp_user, "SMTP_PASS": smtp_pass, "SMTP_FROM": smtp_from,
        }.items() if not v]
        print("email cred.........", missing)
        if missing:
            return (False, f"Missing SMTP config in .env: {', '.join(missing)}")

        smtp_port = int(smtp_port_str)

        msg = MIMEMultipart()
        msg["From"] = smtp_from
        msg["To"] = to_email
        msg["Subject"] = subject
        if cc_emails:
            msg["CC"] = ", ".join(cc_emails)

        msg.attach(MIMEText(body, "plain"))

        all_recipients = [to_email] + cc_emails

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, all_recipients, msg.as_string())

        return (True, "Email sent successfully.")
    except Exception as e:
        return (False, str(e))
