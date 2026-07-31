"""Feed parsing and URL safety for the news watcher.

Everything here is stdlib — `xml.etree.ElementTree` handles RSS 2.0, Atom, and
sitemap XML perfectly well. That's deliberate: requirements.txt is pinned to
four packages to stay well inside a 500 MB Railway instance, and neither
`feedparser` nor `beautifulsoup4` earns its weight for three tag lookups.

These functions are pure (no I/O) so they can be tested against saved payloads.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

# Two sources are built in. Anthropic has no RSS feed at all (both /rss.xml and
# /news/rss.xml 404), so we read its sitemap and filter to /news/ paths — the
# <lastmod> timestamps are enough to spot new posts, and titles get filled in
# afterwards from each new article's og: meta tags.
BUILTIN_FEEDS = {
    "openai": {
        "label": "OpenAI",
        "url": "https://openai.com/news/rss.xml",
        "kind": "rss",
        "path_prefix": "",
    },
    "anthropic": {
        "label": "Anthropic",
        "url": "https://www.anthropic.com/sitemap.xml",
        "kind": "sitemap",
        "path_prefix": "/news/",
    },
}

# Refuse to buffer more than this per feed. Sized against reality, not a guess:
# OpenAI's RSS is ~640 KB / 1000+ items and Anthropic's sitemap ~65 KB, so 4 MiB
# leaves real room for growth while still bounding memory on a 500 MB instance.
MAX_BODY_BYTES = 4_194_304


@dataclass(frozen=True)
class Item:
    guid: str      # stable identity for dedupe (RSS guid, or the URL)
    title: str
    link: str
    published: str  # raw date string as published; display-only
    summary: str


class FeedError(Exception):
    """Feed content we won't or can't parse."""


def _localname(tag: str) -> str:
    """Strip the XML namespace. Feeds vary wildly in which namespaces they
    declare, so matching on the local name keeps this tolerant."""
    return tag.rsplit("}", 1)[-1].lower()


def _find(elem, *names: str):
    """First direct child whose local name matches any of `names`."""
    wanted = {n.lower() for n in names}
    for child in elem:
        if _localname(child.tag) in wanted:
            return child
    return None


def _text(elem, *names: str) -> str:
    child = _find(elem, *names)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _safe_parse(data: bytes):
    """Parse XML, refusing anything with a DOCTYPE.

    ElementTree doesn't expand external entities, but it does process internal
    ones — which is enough for a billion-laughs style expansion. Feeds have no
    legitimate reason to declare a DOCTYPE, so rejecting outright is both safe
    and cheap.
    """
    head = data[:2048].lstrip()
    if b"<!DOCTYPE" in head or b"<!doctype" in head:
        raise FeedError("Refusing to parse XML containing a DOCTYPE declaration.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"Not valid XML: {exc}") from exc


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&#x27;": "'",
}


def _unescape(s: str) -> str:
    for k, v in _ENTITIES.items():
        s = s.replace(k, v)
    return s


def clean_summary(html: str, limit: int = 350) -> str:
    """Flatten a feed's HTML description into plain text for an embed.

    Entities get unescaped *after* tags are stripped: feeds routinely
    double-escape, so XML parsing turns `&lt;p&gt;` into real markup that then
    needs removing, leaving a second layer of `&amp;` behind it.
    """
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()
    text = _unescape(text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def parse_rss(data: bytes) -> list[Item]:
    """Parse RSS 2.0 or Atom into items, newest-first as published.

    Both formats are handled by the same walk because they differ only in tag
    names and in how the link is carried (element text vs. href attribute).
    """
    root = _safe_parse(data)
    channel = _find(root, "channel") or root  # RSS nests in <channel>; Atom doesn't

    items: list[Item] = []
    for node in channel:
        if _localname(node.tag) not in ("item", "entry"):
            continue

        link = _text(node, "link")
        if not link:
            # Atom carries the URL as <link href="..."/>, with no text.
            link_el = _find(node, "link")
            if link_el is not None:
                link = (link_el.get("href") or "").strip()

        guid = _text(node, "guid", "id") or link
        if not guid:
            continue  # nothing stable to dedupe on — skip rather than re-post forever

        items.append(
            Item(
                guid=guid,
                title=_text(node, "title") or "(untitled)",
                link=link or guid,
                published=_text(node, "pubdate", "published", "updated", "date"),
                summary=clean_summary(
                    _text(node, "description", "summary", "content", "encoded")
                ),
            )
        )
    return items


def parse_sitemap(data: bytes, path_prefix: str = "") -> list[Item]:
    """Parse a sitemap.xml into items, filtered to URLs under `path_prefix`.

    Sitemaps carry no titles, so `title` is left blank for the caller to fill in
    from the article page. Sorted newest-first by <lastmod> so that a first-run
    seed and any batch of new posts are both in a sensible order.
    """
    root = _safe_parse(data)
    if _localname(root.tag) == "sitemapindex":
        raise FeedError("This is a sitemap index, not a sitemap of pages.")

    items: list[Item] = []
    for node in root:
        if _localname(node.tag) != "url":
            continue
        loc = _text(node, "loc")
        if not loc:
            continue
        if path_prefix and not urlparse(loc).path.startswith(path_prefix):
            continue
        items.append(
            Item(
                guid=loc,
                title="",
                link=loc,
                published=_text(node, "lastmod"),
                summary="",
            )
        )
    items.sort(key=lambda i: i.published, reverse=True)  # ISO-8601 sorts lexically
    return items


_META_RE = re.compile(
    r"""<meta\s+[^>]*?(?:property|name)\s*=\s*["']og:(title|description)["']"""
    r"""[^>]*?content\s*=\s*["'](.*?)["']""",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_og_meta(html: str) -> tuple[str, str]:
    """Pull (title, description) out of a page's og: meta tags.

    A regex is the right tool here: we want two specific attributes out of the
    <head>, not a document model, and pulling in an HTML parser for it would
    cost more than the feature.
    """
    found = {k.lower(): _unescape(v.strip()) for k, v in _META_RE.findall(html)}
    title = found.get("title", "")
    if not title:
        m = _TITLE_RE.search(html)
        if m:
            title = _unescape(_WS_RE.sub(" ", m.group(1)).strip())
    return title, clean_summary(found.get("description", ""))


# ── URL safety ────────────────────────────────────────────────────────────────

class UnsafeURL(Exception):
    """A URL we refuse to fetch."""


def _ip_is_public(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def assert_safe_url(url: str) -> str:
    """Validate a user-supplied feed URL, or raise UnsafeURL.

    The bot runs on Railway with reach into the platform's internal network, and
    `/news add` lets a server admin choose what it fetches — that is an SSRF
    sink. So: HTTPS only, and every address the hostname resolves to must be
    publicly routable. Callers must re-run this on each redirect target, because
    a perfectly innocent public host can 302 to 169.254.169.254.

    Blocking (it does a DNS lookup) — async callers should use
    `assert_safe_url_async`. Returns the normalized URL.
    """
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeURL("Feed URLs must start with `https://`.")
    if not parsed.hostname:
        raise UnsafeURL("That URL has no hostname.")
    if parsed.port is not None and parsed.port != 443:
        raise UnsafeURL("Only the standard HTTPS port (443) is allowed.")

    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"Couldn't resolve `{parsed.hostname}`.") from exc

    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise UnsafeURL(f"Couldn't resolve `{parsed.hostname}`.")
    for addr in addrs:
        if not _ip_is_public(addr):
            raise UnsafeURL(
                f"`{parsed.hostname}` resolves to a private address — refusing to fetch it."
            )
    return url


async def assert_safe_url_async(url: str) -> str:
    """`assert_safe_url` off the event loop — the DNS lookup inside it is a
    blocking call, and the bot shares its loop with the keep-alive web server."""
    return await asyncio.to_thread(assert_safe_url, url)
