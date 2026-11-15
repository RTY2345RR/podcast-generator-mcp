"""Shows, hosts and episodes, saved as JSON on disk so every step can be resumed.

Layout (under PODCAST_OUTPUT_DIR, default ~/Podcasts):

    shows/<show_id>/show.json
    shows/<show_id>/feed.xml                       (RSS feed of every stitched episode)
    shows/<show_id>/episodes/<episode_id>/episode.json
    shows/<show_id>/episodes/<episode_id>/lines/0001.wav ...
    shows/<show_id>/episodes/<episode_id>/<episode_id>.mp3 / .srt / .chapters.json / .chapters.txt / .rss-item.xml
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlsplit

SLUG_RE = re.compile(r"[^a-z0-9]+")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
MAX_LINE_CHARS = 4000
MAX_LINES = 2000
HOST_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*|__)?\[?(?P<host>[^\]\[:*_\n]{1,40}?)\]?(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(?P<text>.+)$"
)
CHAPTER_RE = re.compile(r"^\s*(?:#{1,3}\s+(?P<h>.+?)\s*#*\s*$|\[(?:chapter|section)\s*:\s*(?P<b>[^\]]+)\]\s*$)", re.I)
TAG_RE = re.compile(r"^\s*\((?P<tags>[^)]{1,40})\)\s*")
BLOCKED_REF_HOSTS = ("huggingface.co", "hf.co")


class ShowError(Exception):
    pass


def slugify(text: str, fallback: str = "show") -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    slug = SLUG_RE.sub("-", norm).strip("-")[:60].strip("-")
    return slug or fallback


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def check_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not ID_RE.match(value):
        raise ShowError(f"{what} {value!r} is not valid: use the id returned by the tool (lowercase letters, digits, -).")
    return value


# ------------------------------------------------------------------ hosts
def clean_host(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ShowError("Each host must be an object like {\"name\": \"Alex\", \"reference_audio_url\": \"https://...\"}.")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 40 or ":" in name or "[" in name or "]" in name:
        raise ShowError(f"Host name {name!r} must be 1-40 characters without ':' or brackets.")
    actor_id = str(raw.get("actor_id") or "").strip()
    url = str(raw.get("reference_audio_url") or "").strip()
    if bool(actor_id) == bool(url):
        raise ShowError(
            f"Host {name}: give exactly one voice source, reference_audio_url (a clip to clone) or actor_id "
            "(a saved voice from podcast_list_voices)."
        )
    host: dict[str, Any] = {"name": name}
    if actor_id:
        host["actor_id"] = actor_id
    else:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ShowError(f"Host {name}: reference_audio_url must be a public http(s) URL of a WAV/MP3 clip.")
        if any(parts.netloc == h or parts.netloc.endswith("." + h) for h in BLOCKED_REF_HOSTS):
            raise ShowError(
                f"Host {name}: the voice engine cannot download Hugging Face URLs. Host the clip elsewhere "
                "(your site, S3, GitHub raw, any public file host)."
            )
        host["reference_audio_url"] = url
        ref_text = str(raw.get("reference_text") or "").strip()
        if ref_text:
            host["reference_text"] = ref_text
    for key in ("model", "language", "description"):
        if raw.get(key):
            host[key] = str(raw[key]).strip()
    if raw.get("speed") is not None:
        try:
            speed = float(raw["speed"])
        except (TypeError, ValueError):
            raise ShowError(f"Host {name}: speed must be a number between 0.25 and 4.") from None
        if not 0.25 <= speed <= 4.0:
            raise ShowError(f"Host {name}: speed must be between 0.25 and 4.")
        host["speed"] = speed
    if raw.get("tags"):
        host["tags"] = [str(t) for t in raw["tags"]][:5]
    return host


# ------------------------------------------------------------------ script
def _match_host(name: str, hosts: list[dict[str, Any]]) -> Optional[str]:
    key = name.strip().lower()
    for h in hosts:
        if h["name"].lower() == key:
            return h["name"]
    return None


def parse_script(script: Union[str, list[Any]], hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn a host-tagged script into lines: [{host, text, chapter?, tags?, pause_after?}].

    Text form, one line per turn:  `Alex: Welcome back!`  (also `[Alex]: ...`, `**Alex:** ...`).
    `## Chapter title` or `[chapter: Title]` starts a chapter. A line without a host tag
    continues the previous turn. A leading `(tag)` such as `(laughs)` is passed as a voice tag.
    List form: [{"host": "Alex", "text": "...", "chapter": "Intro", "pause_after": 0.6}].
    """
    names = ", ".join(h["name"] for h in hosts)
    lines: list[dict[str, Any]] = []
    pending_chapter: Optional[str] = None
    if isinstance(script, str):
        for n, raw in enumerate(script.splitlines(), 1):
            if not raw.strip():
                continue
            ch = CHAPTER_RE.match(raw)
            if ch:
                pending_chapter = (ch.group("h") or ch.group("b") or "").strip()
                continue
            m = HOST_LINE_RE.match(raw)
            host = _match_host(m.group("host"), hosts) if m else None
            if host is None:
                if m and not lines:
                    raise ShowError(f"Script line {n}: unknown host {m.group('host')!r}. Hosts of this show: {names}.")
                if not lines:
                    raise ShowError(f"Script line {n} has no host tag. Write it as `HostName: text` (hosts: {names}).")
                if m and len(m.group("host").split()) == 1 and m.group("host")[:1].isupper():
                    raise ShowError(f"Script line {n}: unknown host {m.group('host')!r}. Hosts of this show: {names}.")
                lines[-1]["text"] += " " + raw.strip()
                continue
            entry: dict[str, Any] = {"host": host, "text": m.group("text").strip()}
            if pending_chapter:
                entry["chapter"] = pending_chapter
                pending_chapter = None
            lines.append(entry)
    elif isinstance(script, list):
        for n, item in enumerate(script, 1):
            if not isinstance(item, dict):
                raise ShowError(f"Script item {n} must be an object with host and text.")
            host = _match_host(str(item.get("host") or item.get("speaker") or ""), hosts)
            if host is None:
                raise ShowError(f"Script item {n}: unknown host {item.get('host')!r}. Hosts of this show: {names}.")
            entry = {"host": host, "text": str(item.get("text") or "").strip()}
            if item.get("chapter"):
                entry["chapter"] = str(item["chapter"]).strip()
            if item.get("tags"):
                entry["tags"] = [str(t) for t in item["tags"]][:5]
            if item.get("pause_after") is not None:
                try:
                    entry["pause_after"] = max(0.0, min(5.0, float(item["pause_after"])))
                except (TypeError, ValueError):
                    raise ShowError(f"Script item {n}: pause_after must be seconds (0-5).") from None
            lines.append(entry)
    else:
        raise ShowError("script must be text (`Host: line` per turn) or a list of {host, text} objects.")

    for n, entry in enumerate(lines, 1):
        tm = TAG_RE.match(entry["text"])
        if tm and "tags" not in entry:
            entry["tags"] = [t.strip() for t in tm.group("tags").split(",") if t.strip()][:5]
            entry["text"] = entry["text"][tm.end():].strip()
        if not entry["text"]:
            raise ShowError(f"Script turn {n} ({entry['host']}) has no text.")
        if len(entry["text"]) > MAX_LINE_CHARS:
            raise ShowError(f"Script turn {n} is {len(entry['text'])} characters; split it under {MAX_LINE_CHARS}.")
    if not lines:
        raise ShowError(f"The script has no host lines. Write turns as `HostName: text` (hosts: {names}).")
    if len(lines) > MAX_LINES:
        raise ShowError(f"The script has {len(lines)} turns; the limit is {MAX_LINES} per episode.")
    return lines


def estimate_seconds(text: str, speed: float = 1.0) -> float:
    words = len(text.split()) or max(1, len(text) // 3)
    return words / 2.6 / max(0.25, speed)  # ~155 words per minute


# ------------------------------------------------------------------ storage
class Store:
    def __init__(self, root: Path) -> None:
        self.root = root

    def show_dir(self, show_id: str) -> Path:
        return self.root / check_id(show_id, "show_id")

    def episode_dir(self, show_id: str, episode_id: str) -> Path:
        return self.show_dir(show_id) / "episodes" / check_id(episode_id, "episode_id")

    def load_show(self, show_id: str) -> dict[str, Any]:
        path = self.show_dir(show_id) / "show.json"
        if not path.exists():
            known = ", ".join(self.list_show_ids()) or "none yet"
            raise ShowError(f"Show {show_id!r} not found. Known shows: {known}. Create one with podcast_create_show.")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_show(self, show: dict[str, Any]) -> None:
        show["updated_at"] = now_iso()
        atomic_write_json(self.show_dir(show["show_id"]) / "show.json", show)

    def load_episode(self, show_id: str, episode_id: str) -> dict[str, Any]:
        path = self.episode_dir(show_id, episode_id) / "episode.json"
        if not path.exists():
            known = ", ".join(self.list_episode_ids(show_id)) or "none yet"
            raise ShowError(f"Episode {episode_id!r} not found in show {show_id!r}. Known episodes: {known}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_episode(self, ep: dict[str, Any]) -> None:
        ep["updated_at"] = now_iso()
        atomic_write_json(self.episode_dir(ep["show_id"], ep["episode_id"]) / "episode.json", ep)

    def list_show_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.parent.name for p in self.root.glob("*/show.json"))

    def list_episode_ids(self, show_id: str) -> list[str]:
        base = self.show_dir(show_id) / "episodes"
        if not base.exists():
            return []
        return sorted(p.parent.name for p in base.glob("*/episode.json"))

    def new_id(self, base: Path, title: str, fallback: str) -> str:
        slug = slugify(title, fallback)
        candidate, n = slug, 2
        while (base / candidate).exists():
            candidate = f"{slug}-{n}"
            n += 1
        return candidate
