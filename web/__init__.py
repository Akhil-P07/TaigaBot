"""Web dashboard: Discord OAuth2 login and the JSON API behind it.

These routes run inside the bot process and read SQLite directly — no separate
service, no shared database. That's a deliberate trade: it keeps the DB a single
file the bot owns, at the cost of the site sharing the bot's event loop. Handlers
here should stay cheap and non-blocking for that reason.
"""
