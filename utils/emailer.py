"""Sends verification OTP emails over Gmail SMTP.

Uses a Google "App Password" (not the account password). smtplib is blocking,
so callers should run `send_otp_email` in a thread (see verification feature,
which uses `asyncio.to_thread`).
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

import config


class EmailError(Exception):
    """Raised when the OTP email could not be sent."""


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
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailError(
            "Gmail rejected the login. Check GMAIL_ADDRESS and that "
            "GMAIL_APP_PASSWORD is a valid App Password."
        ) from e
    except Exception as e:  # noqa: BLE001 - surface any SMTP failure to caller
        raise EmailError(f"Could not send email: {e}") from e
