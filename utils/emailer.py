"""Sends verification OTP emails via Brevo's HTTP API.

We POST to Brevo (https://api.brevo.com) over HTTPS:443 instead of using SMTP,
because many hosts (Railway/Render/Fly containers, …) block outbound SMTP ports
(25/465/587) — there it fails with "Network is unreachable" or a timeout. Port
443 is never blocked, so this works anywhere.

Setup: create a free Brevo account, verify your sender address (Senders & IPs →
no domain required), and create an API key (SMTP & API → API Keys). Put the key
in BREVO_API_KEY and the verified address in EMAIL_FROM.

urllib is blocking, so callers should run `send_otp_email` in a thread (the
verification feature uses `asyncio.to_thread`).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import config

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
HTTP_TIMEOUT = 20  # seconds — don't hang the verify flow


class EmailError(Exception):
    """Raised when the OTP email could not be sent."""


def _html_body(code: str, discord_name: str, guild_name: str) -> str:
    return f"""
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
    """


def send_otp_email(
    to_address: str, code: str, discord_name: str, guild_name: str = "TaigaBot"
) -> None:
    if not config.BREVO_API_KEY:
        raise EmailError("Email is not configured (BREVO_API_KEY).")
    if not config.EMAIL_FROM:
        raise EmailError("Email sender is not configured (EMAIL_FROM / GMAIL_ADDRESS).")

    text_body = (
        f"Hi {discord_name},\n\n"
        f"Your verification code for {guild_name} on Discord is: {code}\n\n"
        f"Enter it in Discord with:  /confirm code:{code}\n\n"
        f"This code expires in {config.OTP_TTL_MINUTES} minutes. "
        f"If you didn't request this, you can ignore this email.\n\n"
        f"— TaigaBot"
    )

    payload = {
        "sender": {"name": config.EMAIL_FROM_NAME, "email": config.EMAIL_FROM},
        "to": [{"email": to_address}],
        "subject": f"Your {guild_name} verification code: {code}",
        "textContent": text_body,
        "htmlContent": _html_body(code, discord_name, guild_name),
    }

    req = urllib.request.Request(
        BREVO_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "api-key": config.BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        # 201 Created on success; nothing useful in the body for us.
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT):
            return
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise EmailError(
                "Brevo rejected the API key — check BREVO_API_KEY."
            ) from e
        if e.code == 400 and "sender" in body.lower():
            raise EmailError(
                f"Brevo won't send from '{config.EMAIL_FROM}'. Verify that address "
                "in Brevo (Senders & IPs) and set EMAIL_FROM to it."
            ) from e
        raise EmailError(f"Brevo error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise EmailError(f"Couldn't reach Brevo ({e.reason}).") from e
    except Exception as e:  # noqa: BLE001 - surface any failure to the caller
        raise EmailError(f"Could not send email: {e}") from e
