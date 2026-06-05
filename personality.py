"""TaigaBot's tsundere personality lines.  ✏️  EDIT ME FREELY!

Everything the bot "says" with attitude lives here so you can tweak the vibe
without touching any logic. Each key is a *situation*; the value is a list of
possible lines. The bot picks one at random.

How to use in code:
    import personality
    personality.say("mention")          # -> a random line for that situation
    personality.say("verify_success", name="Akhil")   # supports {name} etc.

Placeholders you can use in lines (only where noted in the comments):
    {name}  -> the member's display name
    {bot}   -> "TaigaBot"

To add a brand-new situation, just add a new key + list, then call
personality.say("your_key") wherever you want it. Set ENABLED = False to make
the bot behave plainly (no tsundere flavor).

Quite a few lines here RIT mentioned so changed the appropriate lines if deploying to a non-rit club server.
"""
from __future__ import annotations

import random

# Master switch for the personality flavor.
ENABLED = True

# How often (0.0–1.0) the bot tosses in a tsundere remark when simply mentioned.
MENTION_REPLY_CHANCE = 1.0

LINES: dict[str, list[str]] = {
    # When someone @mentions or replies to the bot.
    "mention": [
        "W-what?! It's not like I was waiting for you to talk to me or anything, {name}.",
        "Hmph. You need something? Make it quick.",
        "D-don't get the wrong idea — I'm only helping because I have to!",
        "What is it now? I'm a very busy bot, you know.",
        "Ugh, fine. What do you want, {name}?",
        "Idiot! Don't just ping me for no reason!",
    ],
    # Successful verification (supports {name}).
    "verify_success": [
        "F-fine, you're verified! It's not like I'm happy you joined or anything!",
        "There, done. Don't make me regret letting you in, {name}.",
        "You actually did it right?! ...I mean, of course you did. Welcome.",
        "So you actually have an RIT email. Good. Not that I'm impressed or anything.",
        "Don't you DARE think this means anything!! I verify everyone!! ...but um. Welcome. To my server. Not yours. Mine.",
        "I-it's not like I was refreshing your verification status every second! ...Fine. You're in.",

    ],
    # Wrong OTP code.
    "verify_wrong_code": [
        "That's wrong, dummy! Can you even read your own email?",
        "Nope. Try again and pay attention this time!",
        "Seriously? That's not the code. Hmph.",
    ],
    # When automod deletes someone's message.
    "automod": [
        "Hey! We don't allow that here, got it?",
        "Knock it off! I'm watching you, you know.",
        "Don't make me clean up after you again. Behave!",
    ],
    # When a non-Eboard member tries a mod command.
    "permission_denied": [
        "As if I'd let YOU do that! Only Eboard, got it?",
        "Hah! You don't have the authority, and I don't take orders from just anyone.",
        "Nice try. That's an Eboard-only command, dummy.",
    ],
    # Generic greeting (e.g., /hello).
    "greeting": [
        "Oh, it's you. ...Hi. Whatever.",
        "Hmph. Hello to you too, I suppose.",
        "Don't expect me to be all cheerful about it, but... hi.",
    ],
    # When the bot is thanked.
    "thanks": [
        "Hmph, of course. I'm the best, after all.",
        "Don't mention it. Seriously, don't.",
        "Just doing my job. A job I'm extremely good at, might I add.",
        "Tch. You'd be lost without me. ...Not that I mind. Much.",
        "I-I didn't do it for YOU specifically. ...Glad it helped, though.",
    ],
    # When someone mentions "Ryuji" — Taiga plays dumb (badly).
    "ryuji": [
        "R-Ryuji?! I don't know anyone by that name! Why would you even ask me?!",
        "Who's Ryuji? Never heard of him. ...S-stop looking at me like that!",
        "That name means NOTHING to me, okay?! D-drop it.",
        "Ryuji who? I definitely wasn't thinking about- I mean, no comment!",
    ],
    # When someone asks about her height — instant threat of violence.
    "height": [
        "Say one more word about my height and I'll knock you into next semester!",
        "My height? My fist is about to be at YOUR face level. Keep talking.",
        "Ask about my height again. I dare you. ...See what happens.",
        "Tall enough to reach your nose when I punch it. Wanna test it?!",
    ],
    # Random idle/sass line (used by /taiga).
    "random": [
        "I'm not a regular Discord bot. I'm the Palmtop Tiger, remember that!",
        "Could you NOT? I'm trying to look cool here.",
        "If you've got time to bother me, you've got time to read a paper. Go study!",
        "Hmph. AI Club, huh? At least you have good taste.",
        "Touch grass? No. Touch a textbook. Now shoo.",
        "You're STILL here? Don't you have a model to overfit or whatever?",

    ],
}


def say(situation: str, **fmt) -> str:
    """Return a random line for `situation`, formatted with given kwargs.

    Falls back to an empty string if personality is disabled, or to a neutral
    line if the situation key doesn't exist.
    """
    if not ENABLED:
        return ""
    lines = LINES.get(situation)
    if not lines:
        return ""
    fmt.setdefault("bot", "TaigaBot")
    fmt.setdefault("name", "you")
    try:
        return random.choice(lines).format(**fmt)
    except (KeyError, IndexError):
        return random.choice(lines)
