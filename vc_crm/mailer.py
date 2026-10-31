"""Outgoing email over SMTP (set up for Google Workspace; any SMTP server works).

Configure with environment variables:
    SMTP_USERNAME  the Workspace mailbox that sends, e.g. crm@yourfirm.com
    SMTP_PASSWORD  a Google "app password" for that mailbox (not its normal password)
    MAIL_FROM      optional display sender, defaults to SMTP_USERNAME
    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 587 (STARTTLS)
    APP_URL        the app's public address, used in links, e.g. https://crm.yourfirm.com

Without SMTP_USERNAME/SMTP_PASSWORD nothing is sent; the message is logged instead,
so local testing works without email.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("vc_crm.mail")


def app_url():
    return os.environ.get("APP_URL", "http://localhost:8000").rstrip("/")


def configured():
    return bool(os.environ.get("SMTP_USERNAME") and os.environ.get("SMTP_PASSWORD"))


def send(to, subject, body):
    """Send a plain-text email. Returns True if handed to the SMTP server."""
    if not configured():
        log.warning("Email not configured; would send to %s: %s\n%s", to, subject, body)
        return False
    user = os.environ["SMTP_USERNAME"]
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM", user)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, os.environ["SMTP_PASSWORD"])
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        log.exception("Sending email to %s failed", to)
        return False
