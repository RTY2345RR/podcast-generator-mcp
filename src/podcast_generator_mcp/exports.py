"""Transcript SRT, chapters (Podcasting 2.0 JSON, YouTube-style text) and RSS 2.0 item/feed."""

from __future__ import annotations

import re
from email.utils import format_datetime, parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape, quoteattr

MAX_CUE_CHARS = 84


def ts(seconds: float, sep: str = ",") -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def clock(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _balanced_cut(part: str, target: int, max_chars: int, n: int) -> int:
    """Best cut index near `target`: a clause boundary close by, else the nearest space."""
    best = -1
    tolerance = max(20, target // 2)
    for m in re.finditer(r"[,;:]\s", part):
        fits = m.start() + 1 <= max_chars and len(part) - m.start() - 2 <= (n - 1) * max_chars
        if fits and abs(m.start() - target) <= tolerance and (best < 0 or abs(m.start() - target) < abs(best - target)):
            best = m.start() + 1
    if best > 0:
        return best
    left, right = part.rfind(" ", 0, target), part.find(" ", target)
    options = [i for i in (left, right) if i > 0]
    return min(options, key=lambda i: abs(i - target)) if options else target


def split_cues(text: str, max_chars: int = MAX_CUE_CHARS) -> list[str]:
    """Split a turn into subtitle-sized pieces of balanced length at sentence, clause or word boundaries."""
    pieces: list[str] = []
    for part in re.split(r"(?<=[.!?…])\s+", text.strip()):
        part = part.strip()
        while len(part) > max_chars:
            n = -(-len(part) // max_chars)  # pieces still needed
            cut = _balanced_cut(part, len(part) // n, max_chars, n)
            pieces.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            pieces.append(part)
    merged: list[str] = []
    for p in pieces:  # glue very short sentences together
        if merged and len(merged[-1]) + 1 + len(p) <= max_chars and len(p) < 25:
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged


def build_srt(timeline: list[dict[str, Any]], *, speaker_labels: bool = True) -> str:
    """timeline: [{host, text, start, end}] in seconds. Time inside a turn is shared by characters."""
    out: list[str] = []
    n = 0
    for item in timeline:
        cues = split_cues(item["text"])
        total = sum(len(c) for c in cues) or 1
        start, span = item["start"], max(0.01, item["end"] - item["start"])
        for i, cue in enumerate(cues):
            end = start + span * len(cue) / total
            n += 1
            label = f"{item['host']}: " if speaker_labels and i == 0 else ""
            out.append(f"{n}\n{ts(start)} --> {ts(end)}\n{label}{cue}\n")
            start = end
    return "\n".join(out)


def build_chapters(chapters: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """Podcasting 2.0 JSON chapters (https://github.com/Podcastindex-org/podcast-namespace)."""
    return {
        "version": "1.2.0",
        "title": title,
        "chapters": [{"startTime": round(c["start"], 3), "endTime": round(c["end"], 3), "title": c["title"]} for c in chapters],
    }


def chapters_text(chapters: list[dict[str, Any]]) -> str:
    """YouTube / show-notes style: `00:00 Intro` per line."""
    return "\n".join(f"{clock(c['start'])} {c['title']}" for c in chapters) + ("\n" if chapters else "")


def rfc2822(iso: Optional[str] = None) -> str:
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(iso)
    else:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def _url(base: Optional[str], name: str) -> str:
    if not base:
        return name
    return base.rstrip("/") + "/" + name


def build_rss_item(show: dict[str, Any], ep: dict[str, Any], files: dict[str, Any], base_url: Optional[str]) -> str:
    """One <item> for the show's RSS feed (iTunes + Podcasting 2.0 namespaces)."""
    hosts = ", ".join(h["name"] for h in show.get("hosts", []))
    desc = ep.get("description") or ep.get("title") or ""
    if files.get("chapters_text"):
        desc = desc + "\n\nChapters:\n" + files["chapters_text"].strip()
    lines = [
        "<item>",
        f"  <title>{escape(ep['title'])}</title>",
        f"  <description>{escape(desc)}</description>",
        f"  <itunes:summary>{escape(ep.get('description') or ep['title'])}</itunes:summary>",
        f"  <guid isPermaLink=\"false\">{escape(ep['guid'])}</guid>",
        f"  <pubDate>{rfc2822(ep.get('publish_date'))}</pubDate>",
        f"  <enclosure url={quoteattr(_url(base_url, files['mp3']))} length=\"{files['mp3_bytes']}\" type=\"audio/mpeg\"/>",
        f"  <itunes:duration>{int(round(files['duration']))}</itunes:duration>",
        "  <itunes:episodeType>full</itunes:episodeType>",
        f"  <itunes:explicit>{'true' if show.get('explicit') else 'false'}</itunes:explicit>",
    ]
    if ep.get("episode_number"):
        lines.append(f"  <itunes:episode>{int(ep['episode_number'])}</itunes:episode>")
    if ep.get("season"):
        lines.append(f"  <itunes:season>{int(ep['season'])}</itunes:season>")
    if hosts:
        lines.append(f"  <itunes:author>{escape(show.get('author') or hosts)}</itunes:author>")
    for h in show.get("hosts", []):
        lines.append(f"  <podcast:person role=\"host\">{escape(h['name'])}</podcast:person>")
    lines.append(
        f"  <podcast:transcript url={quoteattr(_url(base_url, files['srt']))} type=\"application/x-subrip\" rel=\"captions\"/>"
    )
    if files.get("chapters"):
        lines.append(f"  <podcast:chapters url={quoteattr(_url(base_url, files['chapters']))} type=\"application/json+chapters\"/>")
    lines.append("</item>")
    return "\n".join(lines) + "\n"


def build_feed(show: dict[str, Any], items: list[str], base_url: Optional[str]) -> str:
    """A complete RSS 2.0 feed that podcast apps and directories accept."""
    ch = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"  <title>{escape(show['title'])}</title>",
        f"  <description>{escape(show.get('description') or show['title'])}</description>",
        f"  <language>{escape(show.get('language') or 'en')}</language>",
        f"  <link>{escape(show.get('website_url') or base_url or '')}</link>",
        f"  <itunes:author>{escape(show.get('author') or ', '.join(h['name'] for h in show.get('hosts', [])))}</itunes:author>",
        f"  <itunes:explicit>{'true' if show.get('explicit') else 'false'}</itunes:explicit>",
        f"  <itunes:category text={quoteattr(show.get('category') or 'Technology')}/>",
        f"  <podcast:guid>{escape(show['guid'])}</podcast:guid>",
        "  <generator>podcast-generator-mcp (https://github.com/RTY2345RR/podcast-generator-mcp)</generator>",
    ]
    if base_url:
        ch.append(f"  <atom:link href={quoteattr(_url(base_url, 'feed.xml'))} rel=\"self\" type=\"application/rss+xml\"/>")
    if show.get("artwork_url"):
        ch.append(f"  <itunes:image href={quoteattr(show['artwork_url'])}/>")
    if show.get("owner_email"):
        ch.append(f"  <itunes:owner><itunes:email>{escape(show['owner_email'])}</itunes:email></itunes:owner>")
    for item in items:
        ch.append(item.rstrip("\n"))  # not re-indented: that would change multi-line descriptions
    ch += ["</channel>", "</rss>", ""]
    return "\n".join(ch)
