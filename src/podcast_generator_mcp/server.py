"""Podcast generator MCP server: your agent writes the two-host script, these tools voice and publish it."""

from __future__ import annotations

import logging
import shutil
import sys
import uuid
from typing import Any, Optional, Union

import httpx

from . import __version__, exports, media, pipeline
from .client import MirageaClient, MirageaError
from .config import MISSING_KEY_MESSAGE, ConfigError, Settings, load_settings
from .show import ShowError, Store, clean_host, estimate_seconds, now_iso, parse_script

try:  # official MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # official MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
from mcp.types import ToolAnnotations

log = logging.getLogger("podcast_generator_mcp")

INSTRUCTIONS = """\
AI podcast generator tools: turn any topic, document or notes into a two-host (or multi-host) podcast episode with cloned host voices, an MP3, a transcript SRT, chapters and an RSS item. Miragea is the voice and voice-cloning engine. YOU (the agent) write the script; these tools voice, stitch and package it.
Workflow (every step is saved to disk and can be re-run):
1. podcast_create_show(title, hosts=[{name, reference_audio_url, reference_text} | {name, actor_id}]) -> show_id. Each host is cloned from a short public clip (5-20 s WAV/MP3; not Hugging Face URLs) or uses a saved voice from podcast_list_voices.
2. Write a natural dialogue: one turn per line as `HostName: text`, `## Chapter title` lines for chapters. Then podcast_submit_script(show_id, title, script) -> episode_id and an estimated length.
3. podcast_synthesize_lines(show_id, episode_id) voices every line (one engine job per line, several in parallel). A cloned line can take a couple of minutes; on a timeout just call it again: it resumes saved jobs and never submits duplicates.
4. podcast_stitch_episode(show_id, episode_id) -> MP3 (loudness-normalized, ID3 chapters), SRT transcript, chapters JSON/TXT, RSS <item> and the show's feed.xml.
Shortcut: podcast_generate_episode runs steps 3 and 4. podcast_episode_status shows progress. To fix a line, submit the edited script again: only changed lines are re-voiced.
"""

mcp = _Server(name="podcast-generator-mcp", instructions=INSTRUCTIONS)

# Test hooks
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_SLEEP: Any = None


def _annotations(read_only: bool, idempotent: bool = True, open_world: bool = True) -> ToolAnnotations:
    return ToolAnnotations.model_validate(
        {"readOnlyHint": read_only, "destructiveHint": False, "idempotentHint": idempotent, "openWorldHint": open_world}
    )


def _settings(need_key: bool = True) -> Settings:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise ToolError(str(exc)) from None
    if need_key and not settings.has_key:
        raise ToolError(MISSING_KEY_MESSAGE)
    return settings


def _store(settings: Settings) -> Store:
    return Store(settings.shows_dir)


def _client_factory(settings: Settings):
    def make() -> MirageaClient:
        kwargs: dict[str, Any] = {"transport": _TRANSPORT}
        if _SLEEP is not None:
            kwargs["sleep"] = _SLEEP
        return MirageaClient(settings, **kwargs)

    return make


def _fail(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (MirageaError, ShowError, media.MediaError, ConfigError)):
        return ToolError(str(exc))
    log.exception("unexpected error")
    return ToolError(f"Unexpected error: {type(exc).__name__}: {exc}")


async def _progress(message: str) -> None:
    log.info(message)


def _show_view(show: dict[str, Any], store: Store) -> dict[str, Any]:
    return {
        "show_id": show["show_id"],
        "title": show["title"],
        "hosts": [
            {k: v for k, v in h.items() if k in ("name", "actor_id", "reference_audio_url", "model", "speed", "language")}
            for h in show["hosts"]
        ],
        "episodes": store.list_episode_ids(show["show_id"]),
        "folder": str(store.show_dir(show["show_id"])),
    }


def _episode_view(store: Store, show: dict[str, Any], ep: dict[str, Any]) -> dict[str, Any]:
    counts = pipeline.summarize(ep)
    done = counts.get("succeeded", 0)
    total = len(ep["lines"])
    if ep.get("status") == "stitched" and done == total:
        next_step = "Done. Upload the MP3/SRT/chapters next to feed.xml, or re-run podcast_stitch_episode with base_url."
    elif done == total:
        next_step = "All lines voiced: call podcast_stitch_episode."
    else:
        next_step = "Call podcast_synthesize_lines (it resumes)."
    out: dict[str, Any] = {
        "show_id": show["show_id"],
        "episode_id": ep["episode_id"],
        "title": ep["title"],
        "status": ep.get("status", "draft"),
        "lines_total": total,
        "lines_voiced": done,
        "status_counts": counts,
        "folder": str(store.episode_dir(show["show_id"], ep["episode_id"])),
        "next_step": next_step,
    }
    failed = [
        {"line": i + 1, "host": ln["host"], **(ln.get("job") or {}).get("error", {})}
        for i, ln in enumerate(ep["lines"])
        if (ln.get("job") or {}).get("status") == "failed"
    ]
    if failed:
        out["failed_lines"] = failed[:20]
    if ep.get("output"):
        out["output"] = ep["output"]
    return out


# ===================================================================== tools
@mcp.tool(
    name="podcast_check_setup",
    description=(
        "Check that podcast generation will work: API key set (value hidden), voice engine reachable, its voice "
        "models, optional voice tags and limits, ffmpeg available, and where shows are saved. Read-only; call first."
    ),
    annotations=_annotations(read_only=True),
)
async def podcast_check_setup() -> dict[str, Any]:
    settings = _settings(need_key=False)
    out: dict[str, Any] = {"version": __version__, "shows_folder": str(settings.shows_dir)}
    try:
        out["ffmpeg"] = media.ffmpeg_exe()
    except media.MediaError as exc:
        out["ffmpeg"] = f"missing: {exc}"
    if not settings.has_key:
        out["api_key"] = "missing"
        out["next_step"] = MISSING_KEY_MESSAGE
        return out
    out["api_key"] = "set"
    try:
        async with _client_factory(settings)() as client:
            caps = await client.capabilities()
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None
    audio = caps.get("audio") or {}
    out["voice_engine"] = {
        "models": [
            {
                "id": m.get("id"),
                "uses_reference_text": m.get("uses_reference_text"),
                "tags": [t.get("label") for t in m.get("tags") or [] if isinstance(t, dict)],
            }
            for m in audio.get("models") or []
            if isinstance(m, dict)
        ],
        "default_model": audio.get("default_model"),
        "voice_sources": audio.get("voice_sources"),
        "max_text_length": audio.get("max_text_length"),
    }
    out["ready"] = not str(out["ffmpeg"]).startswith("missing")
    return out


@mcp.tool(
    name="podcast_list_voices",
    description="List saved voices (actors) on the voice engine account, to use as a host with actor_id. Read-only.",
    annotations=_annotations(read_only=True),
)
async def podcast_list_voices(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    settings = _settings()
    try:
        async with _client_factory(settings)() as client:
            return await client.list_actors(limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_create_show",
    description=(
        "Create a podcast show with its hosts. Each host: {name, reference_audio_url, reference_text} to clone a voice "
        "from a short public clip (5-20 s, one speaker, no music; reference_text = exact words in the clip improves "
        "quality; Hugging Face URLs are not fetchable), or {name, actor_id} for a saved voice. Optional per host: "
        "model, speed (0.25-4), language. Show metadata (author, description, language, category, artwork_url, "
        "website_url, owner_email, base_url for public file URLs, explicit) goes into the RSS feed. Returns show_id."
    ),
    annotations=_annotations(read_only=False, idempotent=False, open_world=False),
)
async def podcast_create_show(
    title: str,
    hosts: list[dict[str, Any]],
    description: str = "",
    author: str = "",
    language: str = "en",
    category: str = "Technology",
    artwork_url: str = "",
    website_url: str = "",
    owner_email: str = "",
    base_url: str = "",
    explicit: bool = False,
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        if not title.strip():
            raise ShowError("title is required.")
        if not hosts:
            raise ShowError("Give at least one host, e.g. [{\"name\": \"Alex\", \"reference_audio_url\": \"https://...\"}].")
        if len(hosts) > 6:
            raise ShowError("A show can have at most 6 hosts.")
        clean = [clean_host(h) for h in hosts]
        names = [h["name"].lower() for h in clean]
        if len(set(names)) != len(names):
            raise ShowError("Host names must be unique.")
        show_id = store.new_id(store.root, title, "show")
        show = {
            "show_id": show_id,
            "guid": str(uuid.uuid4()),
            "title": title.strip(),
            "description": description.strip(),
            "author": author.strip(),
            "language": language.strip() or "en",
            "category": category.strip() or "Technology",
            "artwork_url": artwork_url.strip(),
            "website_url": website_url.strip(),
            "owner_email": owner_email.strip(),
            "base_url": base_url.strip(),
            "explicit": bool(explicit),
            "hosts": clean,
            "created_at": now_iso(),
        }
        store.save_show(show)
        return {**_show_view(show, store), "next_step": "Write the script and call podcast_submit_script."}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_set_host",
    description=(
        "Add a host to a show or replace a host's voice (same fields as in podcast_create_show). Lines already voiced "
        "with the old voice are re-voiced on the next podcast_synthesize_lines."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def podcast_set_host(show_id: str, host: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        show = store.load_show(show_id)
        new = clean_host(host)
        others = [h for h in show["hosts"] if h["name"].lower() != new["name"].lower()]
        if len(others) >= 6:
            raise ShowError("A show can have at most 6 hosts.")
        show["hosts"] = others + [new]
        store.save_show(show)
        return _show_view(show, store)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_submit_script",
    description=(
        "Save a dialogue script as an episode (or update an episode by passing episode_id). script is text with one "
        "turn per line as `HostName: text` (also `[HostName]: text`), `## Chapter title` lines to start chapters, and "
        "an optional leading `(tag)` for a voice tag from podcast_check_setup; or a list of {host, text, chapter?, "
        "tags?, pause_after?}. Write it like real people talk: short turns, reactions, questions, no stage directions. "
        "Returns episode_id, turns per host and an estimated duration. Nothing is voiced yet."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def podcast_submit_script(
    show_id: str,
    title: str,
    script: Union[str, list[dict[str, Any]]],
    description: str = "",
    episode_id: str = "",
    episode_number: Optional[int] = None,
    season: Optional[int] = None,
    publish_date: str = "",
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        show = store.load_show(show_id)
        lines = parse_script(script, show["hosts"])
        episodes_root = store.show_dir(show_id) / "episodes"
        if episode_id:
            ep = store.load_episode(show_id, episode_id)
            # keep finished audio for turns whose host, text and tags did not change
            pool: dict[tuple, list[dict[str, Any]]] = {}
            for prev in ep.get("lines", []):
                if prev.get("job"):
                    pool.setdefault((prev["host"], prev["text"], tuple(prev.get("tags") or ())), []).append(prev)
            for line in lines:
                match = pool.get((line["host"], line["text"], tuple(line.get("tags") or ())))
                if match:
                    prev = match.pop(0)
                    line["job"] = prev["job"]
                    if "duration" in prev:
                        line["duration"] = prev["duration"]
            ep.update({"title": title.strip() or ep["title"], "lines": lines, "status": "draft"})
            ep.pop("output", None)
        else:
            if not title.strip():
                raise ShowError("title is required.")
            new_id = store.new_id(episodes_root, title, "episode")
            ep = {
                "show_id": show_id,
                "episode_id": new_id,
                "guid": str(uuid.uuid5(uuid.UUID(show["guid"]), new_id)),
                "title": title.strip(),
                "lines": lines,
                "status": "draft",
                "created_at": now_iso(),
            }
            if episode_number is None:
                episode_number = len(store.list_episode_ids(show_id)) + 1
        if description.strip():
            ep["description"] = description.strip()
        if episode_number is not None:
            ep["episode_number"] = int(episode_number)
        if season is not None:
            ep["season"] = int(season)
        if publish_date.strip():
            exports.rfc2822(publish_date.strip())  # validate early
            ep["publish_date"] = publish_date.strip()
        store.save_episode(ep)
        speeds = {h["name"]: h.get("speed", 1.0) for h in show["hosts"]}
        per_host: dict[str, int] = {}
        for ln in lines:
            per_host[ln["host"]] = per_host.get(ln["host"], 0) + 1
        est = sum(estimate_seconds(ln["text"], speeds.get(ln["host"], 1.0)) + pipeline.DEFAULT_GAP for ln in lines)
        return {
            "show_id": show_id,
            "episode_id": ep["episode_id"],
            "title": ep["title"],
            "turns": len(lines),
            "turns_per_host": per_host,
            "chapters": [ln["chapter"] for ln in lines if ln.get("chapter")],
            "words": sum(len(ln["text"].split()) for ln in lines),
            "estimated_duration": exports.clock(est),
            "lines_already_voiced": sum(1 for ln in lines if (ln.get("job") or {}).get("status") == "succeeded"),
            "next_step": "Call podcast_synthesize_lines (or podcast_generate_episode for voice + stitch).",
        }
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_synthesize_lines",
    description=(
        "Voice every line of an episode with its host's voice (one voice-engine job per line, several in parallel). "
        "Re-running resumes: finished lines are skipped and running jobs are polled, never resubmitted. Optional "
        "lines=[3, 7] re-voices only those line numbers; model overrides the host model for this run."
    ),
    annotations=_annotations(read_only=False),
)
async def podcast_synthesize_lines(
    show_id: str, episode_id: str, lines: Optional[list[int]] = None, model: str = ""
) -> dict[str, Any]:
    settings = _settings()
    store = _store(settings)
    try:
        show = store.load_show(show_id)
        ep = store.load_episode(show_id, episode_id)
        if lines:
            bad = [n for n in lines if not 1 <= int(n) <= len(ep["lines"])]
            if bad:
                raise ShowError(f"Line numbers out of range 1-{len(ep['lines'])}: {bad}")
            for n in lines:
                ep["lines"][int(n) - 1].pop("job", None)
        result = await pipeline.synthesize(
            store, show, ep, _client_factory(settings),
            concurrency=settings.concurrency, model=model or None,
            only_lines=[int(n) for n in lines] if lines else None, progress=_progress,
        )
        result["episode_id"] = episode_id
        result["next_step"] = (
            "Call podcast_stitch_episode." if result["complete"]
            else "Call podcast_synthesize_lines again to resume (fix failed lines first if errors repeat)."
        )
        return result
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_stitch_episode",
    description=(
        "Stitch the voiced lines into one episode: MP3 (mono 128 kbps, -16 LUFS, ID3 tags and chapters), transcript "
        "SRT with speaker names, plain-text transcript, Podcasting 2.0 chapters JSON + `00:00 Title` text, an RSS "
        "<item> and the show's feed.xml. gap_seconds sets the pause between turns; base_url (public folder URL) makes "
        "enclosure/transcript links absolute. Local only: makes no voice-engine call."
    ),
    annotations=_annotations(read_only=False, open_world=False),
)
async def podcast_stitch_episode(
    show_id: str,
    episode_id: str,
    gap_seconds: float = pipeline.DEFAULT_GAP,
    base_url: str = "",
    speaker_labels: bool = True,
    normalize_loudness: bool = True,
) -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        if not 0 <= gap_seconds <= 5:
            raise ShowError("gap_seconds must be between 0 and 5.")
        show = store.load_show(show_id)
        ep = store.load_episode(show_id, episode_id)
        if base_url.strip():
            show["base_url"] = base_url.strip()
            store.save_show(show)
        return pipeline.stitch_and_export(
            store, show, ep, gap_seconds=gap_seconds, base_url=base_url.strip() or None,
            speaker_labels=speaker_labels, loudness=-16.0 if normalize_loudness else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_generate_episode",
    description=(
        "One call after podcast_submit_script: voice all remaining lines, then stitch and export (MP3, SRT, chapters, "
        "RSS). If some lines are still rendering it returns progress; call it again to resume."
    ),
    annotations=_annotations(read_only=False),
)
async def podcast_generate_episode(show_id: str, episode_id: str, base_url: str = "") -> dict[str, Any]:
    result = await podcast_synthesize_lines(show_id, episode_id)
    if not result["complete"]:
        return result
    out = await podcast_stitch_episode(show_id, episode_id, base_url=base_url)
    return {"lines_total": result["lines_total"], **out}


@mcp.tool(
    name="podcast_episode_status",
    description="Progress of an episode: lines voiced, failed lines with reasons, output files and the next step. Read-only.",
    annotations=_annotations(read_only=True, open_world=False),
)
async def podcast_episode_status(show_id: str, episode_id: str) -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        show = store.load_show(show_id)
        return _episode_view(store, show, store.load_episode(show_id, episode_id))
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_get_script",
    description="Return an episode's saved script as numbered turns (host, text, chapter, voiced or not). Read-only.",
    annotations=_annotations(read_only=True, open_world=False),
)
async def podcast_get_script(show_id: str, episode_id: str) -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        ep = store.load_episode(show_id, episode_id)
        return {
            "episode_id": episode_id,
            "title": ep["title"],
            "lines": [
                {"line": i + 1, "host": ln["host"], "text": ln["text"], "chapter": ln.get("chapter"),
                 "voiced": (ln.get("job") or {}).get("status") == "succeeded"}
                for i, ln in enumerate(ep["lines"])
            ],
        }
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


@mcp.tool(
    name="podcast_list_shows",
    description="List saved shows with their hosts and episode ids. Read-only.",
    annotations=_annotations(read_only=True, open_world=False),
)
async def podcast_list_shows() -> dict[str, Any]:
    settings = _settings(need_key=False)
    store = _store(settings)
    try:
        return {"shows": [_show_view(store.load_show(s), store) for s in store.list_show_ids()],
                "folder": str(store.root)}
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from None


def run() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep result URLs out of client logs
    log.info("podcast-generator-mcp %s starting (stdio)", __version__)
    if not shutil.which("ffmpeg"):
        log.info("ffmpeg not on PATH; using the bundled imageio-ffmpeg binary")
    mcp.run("stdio")
