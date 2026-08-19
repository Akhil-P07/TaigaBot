"""Field-level encryption + blind indexing for TaigaBot's PII.

The database file stores every verified member's real name and RIT email. Across
20+ servers that's a single file linking hundreds of real people to their student
accounts — so a stray backup or a copied volume is a full roster leak. This module
makes the file useless on its own: the key lives only in the bot's environment.

One root secret (ENCRYPTION_KEY) fans out via HKDF-SHA256 into three
domain-separated subkeys, so the same bytes are never reused for two purposes:

    data         AES-256-GCM key for real_name / email / discord_username
    index        HMAC-SHA256 key for the deterministic student-id blind index
    fingerprint  published in the DB so a mismatched key fails fast at startup

Only database.py imports this. Callers never see ciphertext.

Why a blind index at all: `email` is encrypted with a *random* nonce, so equal
emails produce different ciphertext and SQL can neither match nor uniquely
constrain it. Lookups therefore go through `blind_index(student_id)`, which is
deterministic — the classic searchable-encryption trade: the index leaks equality
(you can tell two rows share a student id) but never the value itself.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# "v1:" + urlsafe-base64(nonce || ciphertext || tag). The version prefix exists so
# a future scheme can be introduced without guessing at what old rows contain.
ENVELOPE_PREFIX = "v1:"
NONCE_LEN = 12   # AES-GCM standard; anything else costs a re-derivation internally
TAG_LEN = 16

_INFO_DATA = b"taigabot/v1/field-encryption"
_INFO_INDEX = b"taigabot/v1/blind-index"
_INFO_FP = b"taigabot/v1/key-fingerprint"


class CryptoError(RuntimeError):
    """Base class for every failure in this module."""


class MissingKey(CryptoError):
    """ENCRYPTION_KEY is unset, malformed, or too short."""


class DecryptionError(CryptoError):
    """A stored value would not decrypt — wrong key, moved row, or corruption."""


def _hkdf(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256 with an empty salt.

    Written out in stdlib `hmac` rather than pulled from `cryptography` so the
    derivation is auditable inline — it's 8 lines and the security of every
    subkey rests on it.
    """
    prk = hmac.new(b"\x00" * hashlib.sha256().digest_size, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _b64(raw: str) -> bytes:
    """Decode standard or urlsafe base64, tolerating missing padding."""
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))


def parse_root_key(raw: str) -> bytes:
    """Accept the key as 64 hex chars or as base64. Must yield >= 32 bytes."""
    raw = (raw or "").strip()
    if not raw:
        raise MissingKey(
            "ENCRYPTION_KEY is not set. Generate one with:\n"
            '    python -c "import secrets; print(secrets.token_hex(32))"\n'
            "Then put it in .env (or your host's variables). Losing it makes every "
            "stored name and email permanently unrecoverable."
        )
    for decode in (bytes.fromhex, _b64):
        try:
            key = decode(raw)
        except (ValueError, binascii.Error):
            continue
        if len(key) >= 32:
            return key[:32]
    raise MissingKey(
        "ENCRYPTION_KEY must be at least 32 bytes, given as 64 hex characters or "
        'base64. Generate one with:\n'
        '    python -c "import secrets; print(secrets.token_hex(32))"'
    )


class Keys:
    """The three subkeys derived from one root secret."""

    __slots__ = ("data", "index", "fingerprint", "aead")

    def __init__(self, root: bytes) -> None:
        self.data = _hkdf(root, _INFO_DATA)
        self.index = _hkdf(root, _INFO_INDEX)
        # A hash OF a derived value, so publishing it in the DB reveals nothing
        # about the root key and doesn't help verify brute-force guesses any
        # faster than the stored ciphertext already would.
        self.fingerprint = hashlib.sha256(_hkdf(root, _INFO_FP)).hexdigest()[:32]
        self.aead = AESGCM(self.data)


_keys: Keys | None = None


def load(root_key: str | None = None) -> Keys:
    """Cached key material, derived on first use.

    Pass `root_key` explicitly to re-seed the cache (tests do this to simulate a
    different deployment); otherwise ENCRYPTION_KEY is read from the environment.
    Raises MissingKey when the key is unusable.
    """
    global _keys
    if _keys is None or root_key is not None:
        raw = root_key if root_key is not None else os.getenv("ENCRYPTION_KEY", "")
        _keys = Keys(parse_root_key(raw))
    return _keys


def reset_cache() -> None:
    """Forget the cached keys so the next load() re-reads the environment."""
    global _keys
    _keys = None


def aad(table: str, column: str, row_key: object) -> bytes:
    """Associated data binding a ciphertext to the exact cell it belongs in.

    GCM authenticates this alongside the plaintext, so ciphertext lifted from one
    member's row and pasted into another's fails to decrypt instead of silently
    swapping two people's identities. The cost is that moving a row to a new
    discord_id means re-encrypting it — see Database.transfer_verification.
    """
    return f"{table}:{column}:{row_key}".encode("utf-8")


def encrypt(plaintext: str, associated: bytes) -> str:
    """Encrypt one field value into a "v1:" envelope."""
    keys = load()
    nonce = os.urandom(NONCE_LEN)
    blob = nonce + keys.aead.encrypt(nonce, plaintext.encode("utf-8"), associated)
    return ENVELOPE_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def try_decrypt(value: str, associated: bytes) -> str | None:
    """Plaintext, or None when `value` is not a valid envelope under this key/AAD.

    The "v1:" prefix is only a cheap negative filter, never the decision — the GCM
    tag is. A value counts as ciphertext only if the prefix matches AND it decodes
    as base64 AND it's long enough AND authentication passes, which makes a false
    positive a 2**-128 event. That's what lets a member legitimately named
    "v1:Tiger" round-trip: it fails authentication, so it's treated as plaintext
    and encrypted whole.
    """
    if not isinstance(value, str) or not value.startswith(ENVELOPE_PREFIX):
        return None
    try:
        blob = base64.urlsafe_b64decode(value[len(ENVELOPE_PREFIX):])
    except (binascii.Error, ValueError):
        return None
    if len(blob) < NONCE_LEN + TAG_LEN:
        return None
    try:
        return load().aead.decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], associated).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError):
        return None


def decrypt(value: str, associated: bytes) -> str:
    """Strict decrypt for reads. Raises rather than returning something wrong.

    Reads must never fall back to passing the raw value through: that would render
    base64 into a /whois embed as if it were somebody's real name.
    """
    out = try_decrypt(value, associated)
    if out is None:
        raise DecryptionError(
            "Could not decrypt a stored value. Either ENCRYPTION_KEY is not the key "
            "this row was written with, the row was moved between accounts without "
            "re-encrypting, or the data is corrupt."
        )
    return out


def encrypt_if_plaintext(value: str, associated: bytes) -> str:
    """Encrypt only values that aren't already encrypted.

    Migration helper, and the reason the migration is safe to re-run: applying it
    twice cannot double-encrypt.
    """
    return value if try_decrypt(value, associated) is not None else encrypt(value, associated)


def blind_index(value: str) -> str:
    """Deterministic HMAC-SHA256 (hex) of a canonical student id.

    The empty string maps to itself and is NEVER hashed. warnings.identity_key uses
    '' to mean "this member wasn't verified, match them by Discord id instead". If
    '' hashed to a real value, every unverified member in every server would
    collapse into one shared identity whose global warning count is the sum of
    everybody's — silently, and irreversibly.
    """
    if not value:
        return ""
    return hmac.new(
        load().index, b"student_id\x00" + value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
