"""Shared rate limits for commands that post publicly.

Commands whose reply everyone in the channel can see are the ones worth rate
limiting — without a cooldown a member can flood a channel with bot embeds.
The tiers below are named so the durations live in one place instead of being
hardcoded at each call site.

`CommandOnCooldown` is rendered centrally by bot.py's tree error handler, so
commands using these need no error handling of their own.
"""
from __future__ import annotations

import discord
from discord import app_commands

import config
from utils.checks import member_has_role

# (rate, per_seconds)
CHATTY = (1, 5.0)      # cheap, local, public reply
EXTERNAL = (1, 10.0)   # hits a third-party API
BROWSE = (3, 300.0)    # heavy embed + interactive view


def spam_cooldown(rate: int, per: float):
    """Cooldown of `rate` uses per `per` seconds, keyed per (guild, member).

    Eboard members and server admins are exempt — matching `is_eboard()` —
    because returning None from the factory skips the cooldown entirely.
    Buckets are per-guild so a member's use in one server never rate-limits
    them in another.
    """

    def factory(interaction: discord.Interaction) -> app_commands.Cooldown | None:
        member = interaction.user
        if isinstance(member, discord.Member) and (
            member.guild_permissions.administrator
            or member_has_role(member, config.EBOARD_ROLE_NAME)
        ):
            return None
        return app_commands.Cooldown(rate, per)

    return app_commands.checks.dynamic_cooldown(
        factory, key=lambda i: (i.guild_id, i.user.id)
    )
