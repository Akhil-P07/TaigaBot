"""Sends verification OTP emails over Gmail SMTP.

Uses a Google "App Password" (not the account password). smtplib is blocking,
so callers should run `send_otp_email` in a thread (see verification feature,
which uses `asyncio.to_thread`).

Connections are forced over IPv4. Many hosts (Render/Railway/Fly containers,
etc.) have no working IPv6 route, so letting Python pick the AAAA record for
smtp.gmail.com fails immediately with "[Errno 101] Network is unreachable". We
also fall back from SMTPS:465 to STARTTLS:587 in case one port is throttled.
"""
from __future__ import annotations

import smtplib
import socket
import ssl
from email.message import EmailMessage

import config

SMTP_HOST = "smtp.gmail.com"
SMTP_TIMEOUT = 20  # seconds — don't hang the verify flow if the host is firewalled


class EmailError(Exception):
    """Raised when the OTP email could not be sent."""


class _IPv4SMTP_SSL(smtplib.SMTP_SSL):
    """SMTP_SSL that connects over IPv4 only (dodges ENETUNREACH on hosts with no
    IPv6 route), while still verifying TLS against the real hostname."""

    def _get_socket(self, host, port, timeout):
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.create_connection(addr, timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=self._host)


class _IPv4SMTP(smtplib.SMTP):
    """Plain SMTP (for STARTTLS) that connects over IPv4 only."""

    def _get_socket(self, host, port, timeout):
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        return socket.create_connection(addr, timeout, self.source_address)


def send_otp_email(
    to_address: str, code: str, discord_name: str, guild_name: str = "TaigaBot"
) -> None:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise EmailError("Email is not configured (GMAIL_ADDRESS / GMAIL_APP_PASSWORD).")

    msg = EmailMessage()
    msg["Subject"] = f"Your {guild_name} verification code: {code}"
    msg["From"] = f"TaigaBot <{config.GMAIL_ADDRESS}>"
    msg["To"] = to_address

    msg.set_content(
        f"Hi {discord_name},\n\n"
        f"Your verification code for **{guild_name}** on Discord is: {code}\n\n"
        f"Enter it in Discord with:  /confirm code:{code}\n\n"
        f"This code expires in {config.OTP_TTL_MINUTES} minutes. "
        f"If you didn't request this, you can ignore this email.\n\n"
        f"— TaigaBot"
    )
    msg.add_alternative(
        f"""
        <div style="font-family: Arial, sans-serif; max-width: 480px; margin: auto;">
          <h2 style="color:#E8552D;">{guild_name} — Verification</h2>
          <p>Hi {discord_name},</p>
          <p>Your verification code for <strong>{guild_name}</strong> is:</p>
          <p style="font-size:32px; font-weight:bold; letter-spacing:6px;
                    background:#f4f4f4; padding:16px; text-align:center; border-radius:8px;">
            {code}
          </p>
          <p>In Discord, run <code>/confirm code:{code}</code></p>
          <p style="color:#888;">This code expires in {config.OTP_TTL_MINUTES} minutes.
             If you didn't request this, ignore this email.</p>
          <p style="color:#888;">— TaigaBot 🐯</p>
        </div>
        """,
        subtype="html",
    )

    context = ssl.create_default_context()

    def _login_and_send(server: smtplib.SMTP) -> None:
        server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        server.send_message(msg)

    auth_msg = (
        "Gmail rejected the login. Check GMAIL_ADDRESS and that "
        "GMAIL_APP_PASSWORD is a valid App Password."
    )

    # Primary: implicit TLS on 465. Bad credentials fail the same way on any
    # port, so surface that immediately instead of retrying.
    try:
        with _IPv4SMTP_SSL(SMTP_HOST, 465, context=context, timeout=SMTP_TIMEOUT) as server:
            _login_and_send(server)
        return
    except smtplib.SMTPAuthenticationError as e:
        raise EmailError(auth_msg) from e
    except (OSError, smtplib.SMTPException) as primary_err:
        pass  # likely a network/port issue — try STARTTLS on 587 below

    # Fallback: STARTTLS on 587.
    try:
        with _IPv4SMTP(SMTP_HOST, 587, timeout=SMTP_TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            _login_and_send(server)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailError(auth_msg) from e
    except (OSError, smtplib.SMTPException) as e:
        raise EmailError(
            f"Couldn't reach Gmail SMTP on ports 465 or 587 ({e}). If this host "
            "blocks outbound SMTP, switch to an HTTP email API (e.g. SendGrid/Resend)."
        ) from e
