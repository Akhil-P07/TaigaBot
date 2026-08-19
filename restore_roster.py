"""Restore verified members from #taiga-backups files after a database wipe.

    python restore_roster.py roster-555-20260819.csv [roster-777-....csv ...]

Deliberately a script and not a Discord command: you run this against a fresh
database *before* the bot is trusted to start, and the person doing it needs
shell access to the host anyway (that's where ENCRYPTION_KEY lives).

Collect the newest attachment from each server's #taiga-backups channel and pass
them all at once. Rows are scoped per guild, so the union of every server's file
is the whole roster — a missing file just means those members come back
unverified.

Two things this will not do:

  * Restore under a different key. Every file carries the fingerprint of the
    ENCRYPTION_KEY it was written with, and all of them are checked BEFORE
    anything is written. A wrong key would import rows that can never be
    decrypted, which is worse than an empty table.
  * Overwrite anybody. Import is INSERT OR IGNORE: rows already in the database
    win, because a live row is by definition newer than a backup.

Safe to re-run. Importing the same file twice inserts nothing the second time.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys

import config
import crypto
from database import ENCRYPTED_EXPORT_COLUMNS, Database, EncryptionKeyMismatch

META_PREFIX = "#"
INT_COLUMNS = {"discord_id", "guild_id", "verified_at", "last_recovery_at"}


def parse_backup(path: str) -> tuple[str, list[dict]]:
    """Read one backup file. Returns (key_fingerprint, rows).

    The `#` lines features/backup.py writes are metadata, not CSV. Skipping them
    by prefix (rather than by counting lines) keeps this working if the header
    ever gains a line.
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines()

    fingerprint = ""
    body = []
    for line in lines:
        if line.startswith(META_PREFIX):
            for token in line.lstrip(META_PREFIX).split():
                if token.startswith("key_fingerprint="):
                    fingerprint = token.split("=", 1)[1]
            continue
        body.append(line)

    if not fingerprint:
        raise SystemExit(
            f"{path}: no key_fingerprint header — this doesn't look like a "
            "TaigaBot roster backup."
        )

    reader = csv.DictReader(body)
    missing = set(ENCRYPTED_EXPORT_COLUMNS) - set(reader.fieldnames or [])
    if missing:
        raise SystemExit(f"{path}: missing column(s): {', '.join(sorted(missing))}")

    rows = []
    for n, raw in enumerate(reader, start=2):
        row = {}
        for col in ENCRYPTED_EXPORT_COLUMNS:
            value = (raw.get(col) or "").strip()
            if col in INT_COLUMNS:
                try:
                    row[col] = int(value)
                except ValueError:
                    raise SystemExit(f"{path} line {n}: {col}={value!r} is not a number.")
            else:
                row[col] = value
        rows.append(row)
    return fingerprint, rows


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help="roster-*.csv files from #taiga-backups")
    ap.add_argument("--db", default=None, help=f"database path (default: {config.DB_PATH})")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="parse and key-check every file, then stop without writing",
    )
    args = ap.parse_args()

    try:
        keys = crypto.load()
    except crypto.MissingKey as e:
        raise SystemExit(str(e))

    # Parse and key-check EVERY file before opening the database. A batch that
    # would fail halfway should fail before it has written anything at all.
    parsed = []
    for path in args.files:
        if not os.path.exists(path):
            raise SystemExit(f"{path}: no such file.")
        fingerprint, rows = parse_backup(path)
        if fingerprint != keys.fingerprint:
            raise SystemExit(
                f"{path} was written with a different ENCRYPTION_KEY.\n"
                f"  backup fingerprint : {fingerprint}\n"
                f"  current key        : {keys.fingerprint}\n"
                "Restore with the key this backup was made under — importing it "
                "under the current key would store undecryptable rows."
            )
        parsed.append((path, rows))
        print(f"{path}: {len(rows)} row(s), key OK")

    if args.dry_run:
        print("\nDry run — nothing written.")
        return

    db_path = args.db or config.DB_PATH
    db = Database(db_path)
    await db.connect()
    try:
        total_in = total_skip = 0
        for path, rows in parsed:
            try:
                inserted, skipped = await db.import_encrypted_rows(rows, keys.fingerprint)
            except EncryptionKeyMismatch as e:
                raise SystemExit(str(e))
            total_in += inserted
            total_skip += skipped
            print(f"{os.path.basename(path)}: restored {inserted}, skipped {skipped}")
    finally:
        await db.close()

    print(f"\nRestored {total_in} member(s) into {db_path}; {total_skip} already present.")
    if total_in:
        print("Start the bot and run /whois on someone to confirm decryption works.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
