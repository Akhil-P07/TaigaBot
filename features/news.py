"""/news — watch news feeds and post new articles to a channel.

Works with any site that publishes RSS or Atom. OpenAI and Anthropic ship as
built-in shortcuts because this started as an AI-club bot, but they're just two
preset URLs — a server can follow anything it likes via the Custom source.

Each server picks its own sources and its own destination channel. Two design
points carry the whole cost model:

* **Conditional GET.** We store each feed's ETag/Last-Modified and send them
  back, so the steady state is a bodyless 304. Bandwidth is near zero and there
  is nothing to parse on the vast majority of polls.
* **A URL-keyed poll registry.** `news_feeds` is keyed by URL and shared across
  guilds, so twenty servers watching OpenAI cost one request per cycle, not
  twenty. Polling per-subscription instead would multiply traffic by guild count.

Together those keep this well under a dollar a month even at 20+ guilds.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import time
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import feeds as fe
from utils.checks import is_eboard

log = logging.getLogger("taigabot.news")

USER_AGENT = "TaigaBot/1.0 (Discord news watcher; +https://github.com/)"
MAX_REDIRECTS = 5
# If a feed suddenly presents a pile of unseen items (its URL scheme changed, or
# we were offline for a week), post a few and silently mark the rest seen. Better
# to miss some backlog than to dump 100 embeds into someone's channel.
MAX_POSTS_PER_CYCLE = 5
# Sitemaps have no titles, so each new item needs its own page fetch. Cap that
# per cycle so a big sitemap diff can't turn into a burst of requests.
MAX_ENRICH_PER_CYCLE = 5
BACKOFF_AFTER_FAILURES = 5


class Fetched:
    """Result of a conditional GET."""

    __slots__ = ("not_modified", "body", "etag", "last_modified")

    def __init__(self, not_modified: bool, body: bytes, etag: str, last_modified: str):
        self.not_modified = not_modified
        self.body = body
        self.etag = etag
        self.last_modified = last_modified


class News(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self.poll_feeds.change_interval(minutes=config.NEWS_POLL_MINUTES)
        self.poll_feeds.start()

    def cog_unload(self) -> None:
        self.poll_feeds.cancel()
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    def _get_session(self) -> aiohttp.ClientSession:
        """One reused session, same as features/ask.py — keeps connections alive
        and caches DNS. Redirects are handled by hand so each hop can be
        re-validated, so they're disabled at the session level."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    # ── fetching ──────────────────────────────────────────────────────────────

    async def _fetch(
        self, url: str, etag: str = "", last_modified: str = "", *, expect_xml: bool = True
    ) -> Fetched:
        """Conditional GET with manual redirect handling and a hard body cap.

        Every hop is re-validated with `assert_safe_url`, because a public host
        is free to redirect us at a private address.
        """
        session = self._get_session()
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        current = await fe.assert_safe_url_async(url)
        for _ in range(MAX_REDIRECTS):
            async with session.get(
                current, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status == 304:
                    return Fetched(True, b"", etag, last_modified)

                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise fe.FeedError(f"HTTP {resp.status} with no Location header.")
                    current = await fe.assert_safe_url_async(urljoin(current, location))
                    continue

                if resp.status != 200:
                    raise fe.FeedError(f"HTTP {resp.status}")

                ctype = resp.headers.get("Content-Type", "").lower()
                if expect_xml and not any(
                    t in ctype for t in ("xml", "rss", "atom", "text/plain")
                ):
                    raise fe.FeedError(f"Expected XML, got Content-Type: {ctype or 'none'}")

                # Read in chunks so an enormous (or endless) response can't
                # balloon memory on a small instance.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(16384):
                    total += len(chunk)
                    if total > fe.MAX_BODY_BYTES:
                        raise fe.FeedError(
                            f"Response larger than {fe.MAX_BODY_BYTES // 1024} KiB."
                        )
                    chunks.append(chunk)

                return Fetched(
                    False,
                    b"".join(chunks),
                    resp.headers.get("ETag", ""),
                    resp.headers.get("Last-Modified", ""),
                )

        raise fe.FeedError("Too many redirects.")

    async def _parse_feed(self, kind: str, body: bytes, path_prefix: str) -> list[fe.Item]:
        if kind == "sitemap":
            return fe.parse_sitemap(body, path_prefix)
        return fe.parse_rss(body)

    async def _enrich(self, item: fe.Item, *, fetch: bool = True) -> fe.Item:
        """Fill in a sitemap item's title/summary from the article's og: tags.

        Best-effort: with `fetch=False` (or on any failure) we fall back to the
        URL slug, so a post never goes out titleless just because a page fetch
        failed or we hit the per-cycle enrichment cap.
        """
        title, summary = "", ""
        if fetch:
            try:
                got = await self._fetch(item.link, expect_xml=False)
                title, summary = fe.extract_og_meta(
                    got.body.decode("utf-8", errors="replace")
                )
            except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                log.debug("Couldn't enrich %s: %s", item.link, exc)

        if not title:
            # "claude-opus-5" → "Claude Opus 5"
            slug = urlparse(item.link).path.rstrip("/").rsplit("/", 1)[-1]
            title = slug.replace("-", " ").replace("_", " ").title() or "(untitled)"
        return fe.Item(item.guid, title, item.link, item.published, summary or item.summary)

    # ── polling ───────────────────────────────────────────────────────────────

    @tasks.loop(minutes=15)  # real interval set from config in __init__
    async def poll_feeds(self) -> None:
        now = int(time.time())
        backoff_secs = config.NEWS_POLL_MINUTES * 60 * 10
        for feed in await self.bot.db.get_active_feeds():
            # Back off a persistently broken feed to roughly every 10th cycle
            # rather than retrying a dead URL forever. /news list still shows why.
            if (
                feed["fail_count"] >= BACKOFF_AFTER_FAILURES
                and now - feed["last_polled"] < backoff_secs
            ):
                continue
            try:
                await self._poll_one(feed)
            except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the rest
                log.exception("News poll failed for %s", feed["url"])
                await self.bot.db.record_feed_poll(feed["id"], "", "", error=str(exc))
            await asyncio.sleep(2)  # spread load rather than bursting every feed at once

        pruned = await self.bot.db.prune_news_seen()
        if pruned:
            log.info("Pruned %d old news_seen row(s).", pruned)

    @poll_feeds.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_one(self, feed) -> None:
        got = await self._fetch(feed["url"], feed["etag"], feed["last_modified"])
        if got.not_modified:
            await self.bot.db.record_feed_poll(
                feed["id"], feed["etag"], feed["last_modified"], feed["content_hash"]
            )
            return

        # Neither built-in source sends a validator, so the 304 above almost never
        # fires for them and we get the full body every cycle. Hashing it is the
        # cheap fallback: ~1 ms to hash 640 KB vs ~36 ms to parse it, and the
        # bytes are identical on the overwhelming majority of polls.
        digest = hashlib.sha256(got.body).hexdigest()
        if digest == feed["content_hash"]:
            await self.bot.db.record_feed_poll(
                feed["id"], got.etag, got.last_modified, digest
            )
            return

        items = await self._parse_feed(feed["kind"], got.body, feed["path_prefix"])
        await self.bot.db.record_feed_poll(
            feed["id"], got.etag, got.last_modified, digest
        )
        if not items:
            return

        by_guid = {i.guid: i for i in items}
        new_guids = await self.bot.db.filter_unseen(feed["id"], [i.guid for i in items])
        if not new_guids:
            return

        to_post = new_guids[:MAX_POSTS_PER_CYCLE]
        skipped = new_guids[MAX_POSTS_PER_CYCLE:]
        if skipped:
            log.info(
                "Feed %s had %d new items; posting %d and marking the rest seen.",
                feed["url"], len(new_guids), len(to_post),
            )

        # Mark everything seen BEFORE posting. A crash mid-fanout then means a
        # missed article, not a channel full of duplicates on the next cycle.
        await self.bot.db.mark_seen(feed["id"], new_guids)

        subs = await self.bot.db.get_subs_for_feed(feed["id"])
        enriched = 0
        for guid in to_post:
            item = by_guid[guid]
            if not item.title:
                item = await self._enrich(item, fetch=enriched < MAX_ENRICH_PER_CYCLE)
                enriched += 1
            await self._fanout(feed, item, subs)

    async def _fanout(self, feed, item: fe.Item, subs) -> None:
        embed = self._build_embed(feed, item)
        for sub in subs:
            guild = self.bot.get_guild(sub["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(sub["channel_id"])
            # Same guard as features/backup.py: confirm the channel is still in
            # this guild and still postable before trying.
            if channel is None or getattr(channel, "guild", None) != guild:
                log.warning(
                    "News channel %s is gone in guild %s; skipping.",
                    sub["channel_id"], sub["guild_id"],
                )
                continue
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                log.warning(
                    "Can't post news in #%s (guild %s) — missing permissions.",
                    channel.name, guild.id,
                )
                continue
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                log.warning("Forbidden posting news to #%s (guild %s).", channel.name, guild.id)
            except discord.HTTPException:
                log.exception("Failed posting news to #%s (guild %s).", channel.name, guild.id)

    @staticmethod
    def _source_name(feed) -> str:
        for meta in fe.BUILTIN_FEEDS.values():
            if meta["url"] == feed["url"]:
                return meta["label"]
        return urlparse(feed["url"]).hostname or "News"

    def _build_embed(self, feed, item: fe.Item) -> discord.Embed:
        embed = discord.Embed(
            title=item.title[:256],
            url=item.link,
            description=item.summary or None,
            color=config.BOT_COLOR,
        )
        embed.set_author(name=f"📰 {self._source_name(feed)}")
        if item.published:
            embed.set_footer(text=item.published)
        return embed

    # ── commands ──────────────────────────────────────────────────────────────

    news = app_commands.Group(
        name="news", description="Follow any news site or blog feed (Eboard only)."
    )

    _SOURCE_CHOICES = [
        app_commands.Choice(name="OpenAI", value="openai"),
        app_commands.Choice(name="Anthropic", value="anthropic"),
        app_commands.Choice(name="Custom RSS/Atom URL", value="custom"),
    ]

    @news.command(name="add", description="Post a news source's new articles to a channel.")
    @app_commands.describe(
        source="Which news source to watch",
        channel="Where to post new articles",
        url="For 'Custom': the https:// RSS or Atom feed URL",
    )
    @app_commands.choices(source=_SOURCE_CHOICES)
    @is_eboard()
    async def news_add(
        self,
        interaction: discord.Interaction,
        source: app_commands.Choice[str],
        channel: discord.TextChannel,
        url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                f"⚠️ I need **View Channel**, **Send Messages** and **Embed Links** "
                f"in {channel.mention}.",
                ephemeral=True,
            )
            return

        if source.value == "custom":
            if not url:
                await interaction.followup.send(
                    "⚠️ Pick a `url` when using the custom source.", ephemeral=True
                )
                return
            limit = (
                config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS
                if await self.bot.db.is_premium(interaction.guild_id)
                else config.NEWS_MAX_CUSTOM_FEEDS
            )
            if await self.bot.db.count_custom_feeds(interaction.guild_id) >= limit:
                extra = (
                    ""
                    if limit == config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS
                    else f" Premium servers can add up to {config.NEWS_PREMIUM_MAX_CUSTOM_FEEDS}."
                )
                await interaction.followup.send(
                    f"⚠️ This server is at its limit of **{limit}** custom feeds. "
                    f"Remove one first with `/news remove`.{extra}",
                    ephemeral=True,
                )
                return
            try:
                feed_url = await fe.assert_safe_url_async(url)
            except fe.UnsafeURL as exc:
                await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
                return
            kind, path_prefix, label = "rss", "", "custom"
        else:
            meta = fe.BUILTIN_FEEDS[source.value]
            feed_url = meta["url"]
            kind = meta["kind"]
            path_prefix = meta["path_prefix"]
            label = meta["label"]

        # Validate by actually fetching and parsing once, so a bad URL fails
        # loudly here rather than silently in the background loop forever.
        try:
            got = await self._fetch(feed_url)
            items = await self._parse_feed(kind, got.body, path_prefix)
        except (fe.FeedError, fe.UnsafeURL) as exc:
            await interaction.followup.send(f"⚠️ Couldn't read that feed: {exc}", ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            log.exception("news add: fetch failed for %s", feed_url)
            await interaction.followup.send(
                "⚠️ Couldn't reach that URL. Check it and try again.", ephemeral=True
            )
            return

        if not items:
            await interaction.followup.send(
                "⚠️ That URL parsed, but contained no feed items.", ephemeral=True
            )
            return

        feed_id = await self.bot.db.upsert_feed(feed_url, kind, path_prefix)
        await self.bot.db.add_news_sub(interaction.guild_id, feed_id, channel.id, label)
        await self.bot.db.record_feed_poll(
            feed_id, got.etag, got.last_modified, hashlib.sha256(got.body).hexdigest()
        )

        # Cold start: everything currently in the feed counts as already seen, so
        # subscribing doesn't dump the entire back catalogue into the channel.
        seeded = await self.bot.db.filter_unseen(feed_id, [i.guid for i in items])
        await self.bot.db.mark_seen(feed_id, seeded)

        await interaction.followup.send(
            f"✅ Watching **{label if label != 'custom' else feed_url}** — new articles "
            f"will go to {channel.mention}.\n"
            f"Caught up on {len(items)} existing item(s) without posting them; "
            f"checking every {config.NEWS_POLL_MINUTES} minutes.",
            ephemeral=True,
        )

    @news.command(name="remove", description="Stop watching a news source.")
    @app_commands.describe(source="Which subscription to remove (from /news list)")
    @is_eboard()
    async def news_remove(self, interaction: discord.Interaction, source: str):
        subs = await self.bot.db.get_guild_news_subs(interaction.guild_id)
        match = next(
            (s for s in subs if str(s["feed_id"]) == source.strip() or s["url"] == source.strip()),
            None,
        )
        if match is None:
            await interaction.response.send_message(
                "⚠️ No such subscription here — check `/news list`.", ephemeral=True
            )
            return
        await self.bot.db.remove_news_sub(interaction.guild_id, match["feed_id"])
        await interaction.response.send_message(
            f"🛑 Stopped watching `{match['url']}`.", ephemeral=True
        )

    @news_remove.autocomplete("source")
    async def _remove_autocomplete(self, interaction: discord.Interaction, current: str):
        subs = await self.bot.db.get_guild_news_subs(interaction.guild_id)
        out = []
        for s in subs:
            name = f"{s['label']} — {s['url']}"[:100]
            if current.lower() in name.lower():
                out.append(app_commands.Choice(name=name, value=str(s["feed_id"])))
        return out[:25]

    @news.command(name="list", description="Show this server's news subscriptions.")
    @is_eboard()
    async def news_list(self, interaction: discord.Interaction):
        subs = await self.bot.db.get_guild_news_subs(interaction.guild_id)
        if not subs:
            await interaction.response.send_message(
                "No news sources yet — add one with `/news add`.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📰 News subscriptions ({len(subs)})", color=config.BOT_COLOR
        )
        for s in subs[:25]:
            channel = interaction.guild.get_channel(s["channel_id"])
            lines = [
                f"→ {channel.mention if channel else '⚠️ *channel missing*'}",
                f"`{s['url']}`",
                f"Last checked: " + (f"<t:{s['last_polled']}:R>" if s["last_polled"] else "never"),
            ]
            if s["fail_count"]:
                lines.append(f"⚠️ {s['fail_count']} failure(s): {s['last_error'] or 'unknown'}")
            embed.add_field(name=s["label"] or "custom", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"Checked every {config.NEWS_POLL_MINUTES} minutes.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @news.command(
        name="test", description="Preview a source's latest article without posting it."
    )
    @app_commands.describe(source="Which source to preview", url="For 'Custom': the feed URL")
    @app_commands.choices(source=_SOURCE_CHOICES)
    @is_eboard()
    async def news_test(
        self,
        interaction: discord.Interaction,
        source: app_commands.Choice[str],
        url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if source.value == "custom":
            if not url:
                await interaction.followup.send(
                    "⚠️ Pick a `url` when using the custom source.", ephemeral=True
                )
                return
            try:
                feed_url = await fe.assert_safe_url_async(url)
            except fe.UnsafeURL as exc:
                await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
                return
            kind, path_prefix = "rss", ""
        else:
            meta = fe.BUILTIN_FEEDS[source.value]
            feed_url, kind, path_prefix = meta["url"], meta["kind"], meta["path_prefix"]

        try:
            got = await self._fetch(feed_url)
            items = await self._parse_feed(kind, got.body, path_prefix)
        except (fe.FeedError, fe.UnsafeURL) as exc:
            await interaction.followup.send(f"⚠️ Couldn't read that feed: {exc}", ephemeral=True)
            return
        except Exception:  # noqa: BLE001
            log.exception("news test: fetch failed for %s", feed_url)
            await interaction.followup.send("⚠️ Couldn't reach that URL.", ephemeral=True)
            return

        if not items:
            await interaction.followup.send("⚠️ No items in that feed.", ephemeral=True)
            return

        item = items[0]
        if not item.title:
            item = await self._enrich(item)
        fake_feed = {"url": feed_url}
        await interaction.followup.send(
            content=f"Preview — {len(items)} item(s) in this feed. Nothing was posted or marked seen.",
            embed=self._build_embed(fake_feed, item),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(News(bot))
