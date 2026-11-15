"""Episode pipeline: synthesize each host line (resumable), stitch the MP3, export SRT, chapters and RSS."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

from . import exports, media
from .client import MirageaClient, MirageaError, new_idempotency_key
from .show import ShowError, Store

DEFAULT_GAP = 0.35  # seconds between turns
CHAPTER_GAP = 0.9  # extra breath before a new chapter


def fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def voice_payload(host: dict[str, Any], line: dict[str, Any], model: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": line["text"], "format": "wav"}
    if host.get("actor_id"):
        payload["actor_id"] = host["actor_id"]
    else:
        payload["reference_audio_url"] = host["reference_audio_url"]
        if host.get("reference_text"):
            payload["reference_text"] = host["reference_text"]
    chosen = model or host.get("model")
    if chosen:
        payload["model"] = chosen
    if host.get("speed"):
        payload["speed"] = host["speed"]
    tags = line.get("tags") or host.get("tags")
    if tags:
        payload["tags"] = list(tags)
    if host.get("language"):
        payload["language"] = host["language"]
    return payload


def line_file(ep_dir: Path, line: dict[str, Any]) -> Path:
    """Audio of a voiced line, named by its request fingerprint so unchanged lines survive script edits."""
    return ep_dir / "lines" / f"{(line.get('job') or {}).get('fingerprint', 'none')}.wav"


def summarize(ep: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for line in ep["lines"]:
        state = (line.get("job") or {}).get("status", "pending")
        counts[state] = counts.get(state, 0) + 1
    return counts


async def synthesize(
    store: Store,
    show: dict[str, Any],
    ep: dict[str, Any],
    client_factory: Callable[[], MirageaClient],
    *,
    concurrency: int = 4,
    model: Optional[str] = None,
    only_lines: Optional[list[int]] = None,
    progress: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Voice every line that has no finished audio. Re-running resumes: saved task ids are polled, not resubmitted.

    Each line remembers a fingerprint of its request; editing the text or the host voice
    re-voices only the affected lines.
    """
    hosts = {h["name"]: h for h in show["hosts"]}
    ep_dir = store.episode_dir(show["show_id"], ep["episode_id"])
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, concurrency))
    errors: list[dict[str, Any]] = []
    wanted = set(only_lines) if only_lines else None

    async def save() -> None:
        async with lock:
            store.save_episode(ep)

    async def one(index: int, line: dict[str, Any], client: MirageaClient) -> None:
        host = hosts.get(line["host"])
        if host is None:
            raise ShowError(f"Line {index + 1} uses host {line['host']!r}, which is not on the show any more.")
        payload = voice_payload(host, line, model)
        fp = fingerprint(payload)
        job = line.get("job") or {}
        if job.get("fingerprint") == fp and job.get("status") == "succeeded" and line_file(ep_dir, line).exists():
            return
        if job.get("fingerprint") != fp or job.get("status") == "failed" or job.get("status") == "succeeded":
            job = {"fingerprint": fp, "idempotency_key": new_idempotency_key(), "status": "pending"}
            line["job"] = job
            line.pop("duration", None)
        out = line_file(ep_dir, line)
        async with sem:
            try:
                if not job.get("task_id"):
                    task = await client.create_audio(payload, job["idempotency_key"])
                    job["task_id"] = task.get("task_id")
                    job["status"] = task.get("status", "queued")
                    await save()
                    if not job["task_id"]:
                        raise MirageaError("invalid_response", "The voice engine returned no task_id.")
                task = await client.wait_for_task(job["task_id"], progress=progress)
                if task.get("status") != "succeeded":
                    err = task.get("error") or {}
                    job["status"] = "failed"
                    job["error"] = {"code": err.get("code", "failed"), "message": err.get("message", "synthesis failed")}
                    errors.append({"line": index + 1, "host": line["host"], **job["error"]})
                    await save()
                    return
                url = ((task.get("result") or {}).get("audio_url")) or task.get("audio_url")
                if not url:
                    raise MirageaError("invalid_result", "The finished task has no audio_url.")
                out.unlink(missing_ok=True)
                saved = await client.download(url, out)
                job["status"] = "succeeded"
                job.pop("error", None)
                line["duration"] = round(media.probe_duration(saved), 3)
                await save()
            except MirageaError as exc:
                if exc.code == "wait_timeout":
                    job["status"] = "running"
                    await save()
                    errors.append({"line": index + 1, "host": line["host"], "code": exc.code,
                                   "message": "still rendering; run the same tool again to resume"})
                    return
                job["status"] = "failed" if not exc.retryable else job.get("status", "pending")
                job["error"] = {"code": exc.code, "message": exc.message}
                errors.append({"line": index + 1, "host": line["host"], "code": exc.code, "message": exc.message})
                await save()

    # identical turns (same host, text and voice) share one job and one audio file
    first: dict[str, int] = {}
    duplicates: list[tuple[int, int]] = []
    todo: list[int] = []
    for i, line in enumerate(ep["lines"]):
        if wanted is not None and (i + 1) not in wanted:
            continue
        host = hosts.get(line["host"])
        fp = fingerprint(voice_payload(host, line, model)) if host else f"missing-{i}"
        if fp in first:
            duplicates.append((i, first[fp]))
        else:
            first[fp] = i
            todo.append(i)
    async with client_factory() as client:
        await asyncio.gather(*(one(i, ep["lines"][i], client) for i in todo))
    for dup, src in duplicates:
        ep["lines"][dup]["job"] = dict(ep["lines"][src].get("job") or {})
        if "duration" in ep["lines"][src]:
            ep["lines"][dup]["duration"] = ep["lines"][src]["duration"]
    store.save_episode(ep)
    counts = summarize(ep)
    done = counts.get("succeeded", 0)
    return {
        "lines_total": len(ep["lines"]),
        "lines_voiced": done,
        "status_counts": counts,
        "complete": done == len(ep["lines"]),
        "errors": errors[:20],
    }


def stitch_and_export(
    store: Store,
    show: dict[str, Any],
    ep: dict[str, Any],
    *,
    gap_seconds: float = DEFAULT_GAP,
    base_url: Optional[str] = None,
    speaker_labels: bool = True,
    loudness: Optional[float] = -16.0,
) -> dict[str, Any]:
    """Concatenate the voiced lines with natural gaps, encode one MP3 with ID3 chapters, write SRT, chapters, RSS."""
    ep_dir = store.episode_dir(show["show_id"], ep["episode_id"])
    missing = [i + 1 for i, ln in enumerate(ep["lines"])
               if (ln.get("job") or {}).get("status") != "succeeded" or not line_file(ep_dir, ln).exists()]
    if missing:
        raise ShowError(
            f"{len(missing)} line(s) have no audio yet (e.g. lines {missing[:10]}). Run podcast_synthesize_lines first; "
            "it resumes where it stopped."
        )
    slug = ep["episode_id"]
    wav_path = ep_dir / f".{slug}.stitch.wav"
    writer = media.WavWriter(wav_path)
    timeline: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    try:
        for i, line in enumerate(ep["lines"]):
            if i > 0:
                prev = ep["lines"][i - 1]
                pause = prev.get("pause_after")
                if pause is None:
                    pause = gap_seconds + (CHAPTER_GAP if line.get("chapter") else 0.0)
                writer.silence(pause)
            pcm = media.decode_line(line_file(ep_dir, line))
            start = writer.seconds
            writer.pcm(pcm)
            timeline.append({"host": line["host"], "text": line["text"], "start": start, "end": writer.seconds})
            if line.get("chapter") or i == 0:
                if chapters:
                    chapters[-1]["end"] = start
                chapters.append({"title": line.get("chapter") or "Intro", "start": 0.0 if i == 0 else start, "end": None})
        writer.silence(0.6)
    finally:
        writer.close()
    total = writer.seconds
    if chapters:
        chapters[-1]["end"] = total
    if len(chapters) == 1 and not ep["lines"][0].get("chapter"):
        chapters = []  # no chapter markers in the script: don't invent a lone "Intro"

    files: dict[str, Any] = {"mp3": f"{slug}.mp3", "srt": f"{slug}.srt"}
    meta_path = ep_dir / f".{slug}.ffmeta"
    tags = {
        "title": ep["title"],
        "artist": ", ".join(h["name"] for h in show["hosts"]),
        "album": show["title"],
        "album_artist": show.get("author") or "",
        "genre": "Podcast",
        "track": str(ep.get("episode_number") or ""),
        "comment": (ep.get("description") or "")[:500],
        "date": (ep.get("publish_date") or "")[:4],
    }
    meta_path.write_text(media.ffmetadata(tags, [(c["start"], c["end"], c["title"]) for c in chapters]), encoding="utf-8")
    mp3 = ep_dir / files["mp3"]
    try:
        media.encode_episode(wav_path, mp3, metadata_file=meta_path, loudness=loudness)
    finally:
        wav_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    files["duration"] = round(media.probe_duration(mp3), 3)
    files["mp3_bytes"] = mp3.stat().st_size

    (ep_dir / files["srt"]).write_text(exports.build_srt(timeline, speaker_labels=speaker_labels), encoding="utf-8")
    transcript = "\n\n".join(f"{t['host']}: {t['text']}" for t in timeline) + "\n"
    files["transcript"] = f"{slug}.txt"
    (ep_dir / files["transcript"]).write_text(transcript, encoding="utf-8")
    if chapters:
        files["chapters"] = f"{slug}.chapters.json"
        (ep_dir / files["chapters"]).write_text(
            json.dumps(exports.build_chapters(chapters, ep["title"]), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        files["chapters_text"] = exports.chapters_text(chapters)
        (ep_dir / f"{slug}.chapters.txt").write_text(files["chapters_text"], encoding="utf-8")
    base_url = base_url or show.get("base_url")
    item = exports.build_rss_item(show, ep, files, base_url)
    files["rss_item"] = f"{slug}.rss-item.xml"
    (ep_dir / files["rss_item"]).write_text(item, encoding="utf-8")

    ep["output"] = {k: v for k, v in files.items() if k != "chapters_text"}
    ep["chapters"] = chapters
    ep["status"] = "stitched"
    store.save_episode(ep)
    feed = write_feed(store, show, base_url)
    return {
        "mp3": str(mp3),
        "duration_seconds": files["duration"],
        "duration": exports.clock(files["duration"]),
        "srt": str(ep_dir / files["srt"]),
        "transcript": str(ep_dir / files["transcript"]),
        "chapters_json": str(ep_dir / files["chapters"]) if chapters else None,
        "chapters": [{"start": exports.clock(c["start"]), "title": c["title"]} for c in chapters],
        "rss_item": str(ep_dir / files["rss_item"]),
        "feed": str(feed),
        "base_url": base_url,
    }


def write_feed(store: Store, show: dict[str, Any], base_url: Optional[str]) -> Path:
    """Rebuild shows/<id>/feed.xml from every stitched episode (newest first).

    Enclosure URLs are `<base_url>/<episode files>`; upload each episode folder's files
    next to feed.xml, or pass the public folder URL as base_url.
    """
    items = []
    episodes = [store.load_episode(show["show_id"], e) for e in store.list_episode_ids(show["show_id"])]
    episodes = [e for e in episodes if e.get("status") == "stitched" and e.get("output")]
    episodes.sort(key=lambda e: (e.get("publish_date") or e.get("created_at") or ""), reverse=True)
    for e in episodes:
        files = dict(e["output"])
        if e.get("chapters"):
            files["chapters_text"] = exports.chapters_text(e["chapters"])
        items.append(exports.build_rss_item(show, e, files, base_url))
    path = store.show_dir(show["show_id"]) / "feed.xml"
    path.write_text(exports.build_feed(show, items, base_url), encoding="utf-8")
    return path
