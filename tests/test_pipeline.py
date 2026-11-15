import json
import xml.dom.minidom
from pathlib import Path

import pytest

from podcast_generator_mcp import server
from podcast_generator_mcp.media import probe_duration

from .conftest import API_KEY, HOSTS, REF_TEXT, call

SCRIPT = """## Intro
Maya: Welcome to Signal and Noise, the show about tools that talk.
Leo: And today we turn a script into a full episode.
## How it works
Maya: (笑着说) Every line gets its own cloned voice.
Leo: Then everything is stitched into one MP3."""


def make_episode(tmp_path):
    show = call(server.podcast_create_show("Signal and Noise", HOSTS, author="Demo", base_url="https://cdn.example.com/sn"))
    ep = call(server.podcast_submit_script(show["show_id"], "Episode one: hello", SCRIPT, description="Pilot."))
    return show, ep


def test_full_flow(engine, tmp_path):
    show, ep = make_episode(tmp_path)
    assert show["show_id"] == "signal-and-noise"
    assert ep["turns"] == 4 and ep["turns_per_host"] == {"Maya": 2, "Leo": 2}
    assert ep["chapters"] == ["Intro", "How it works"]

    res = call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"]))
    assert res["complete"] and res["lines_voiced"] == 4
    assert len(engine.created) == 4
    first = engine.created[0]
    assert first["reference_audio_url"] == HOSTS[0]["reference_audio_url"] and first["reference_text"] == REF_TEXT
    assert engine.created[1]["speed"] == 1.05
    assert engine.created[2]["tags"] == ["笑着说"] and engine.created[2]["text"].startswith("Every line")

    out = call(server.podcast_stitch_episode(show["show_id"], ep["episode_id"]))
    mp3 = Path(out["mp3"])
    assert mp3.exists() and mp3.read_bytes()[:3] == b"ID3"
    assert 4.0 < probe_duration(mp3) < 8.0
    srt = Path(out["srt"]).read_text()
    assert srt.count("-->") >= 4 and "Maya: Welcome" in srt and "Leo: Then" in srt
    chapters = json.loads(Path(out["chapters_json"]).read_text())
    assert [c["title"] for c in chapters["chapters"]] == ["Intro", "How it works"]
    assert chapters["chapters"][0]["startTime"] == 0 and chapters["chapters"][1]["startTime"] > 2
    item = Path(out["rss_item"]).read_text()
    assert 'url="https://cdn.example.com/sn/episode-one-hello.mp3"' in item and "<itunes:duration>" in item
    feed = Path(out["feed"]).read_text()
    xml.dom.minidom.parseString(feed)  # well-formed
    assert "<itunes:episode>1</itunes:episode>" in feed and "podcast:transcript" in feed

    status = call(server.podcast_episode_status(show["show_id"], ep["episode_id"]))
    assert status["status"] == "stitched" and status["lines_voiced"] == 4


def test_resume_never_resubmits(engine, tmp_path):
    show, ep = make_episode(tmp_path)
    engine.hold = True
    server_settings = server._settings()
    # make the wait time out immediately
    import podcast_generator_mcp.server as srv
    orig = srv.load_settings

    def quick():
        s = orig()
        return s.__class__(**{**s.__dict__, "wait_timeout": 0.001})

    srv.load_settings = quick
    try:
        res = call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"]))
        assert not res["complete"] and len(engine.created) == 4
        assert all(e["code"] == "wait_timeout" for e in res["errors"])
        engine.release()
        res = call(server.podcast_generate_episode(show["show_id"], ep["episode_id"]))
        assert len(engine.created) == 4  # polled, not resubmitted
        assert Path(res["mp3"]).exists()
    finally:
        srv.load_settings = orig
    assert server_settings.has_key


def test_editing_script_revoices_only_changed_lines(engine, tmp_path):
    show, ep = make_episode(tmp_path)
    call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"]))
    edited = SCRIPT.replace("Then everything is stitched into one MP3.", "Then it is all stitched together.")
    edited += "\nMaya: Thanks for listening!"
    upd = call(server.podcast_submit_script(show["show_id"], "", edited, episode_id=ep["episode_id"]))
    assert upd["lines_already_voiced"] == 3 and upd["turns"] == 5
    call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"]))
    assert len(engine.created) == 6
    # changing a host voice re-voices that host's lines
    call(server.podcast_set_host(show["show_id"], {"name": "Leo", "actor_id": "42"}))
    call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"]))
    assert len(engine.created) == 8 and engine.created[-1]["actor_id"] == "42"


def test_failed_line_is_reported_and_stitch_refuses(engine, tmp_path):
    engine.fail_text = "cloned voice"
    show, ep = make_episode(tmp_path)
    res = call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"]))
    assert not res["complete"] and res["errors"][0]["line"] == 3
    with pytest.raises(Exception, match="no audio yet"):
        call(server.podcast_stitch_episode(show["show_id"], ep["episode_id"]))
    engine.fail_text = None
    res = call(server.podcast_synthesize_lines(show["show_id"], ep["episode_id"], lines=[3]))
    assert res["complete"]


def test_check_setup_and_voices(engine):
    out = call(server.podcast_check_setup())
    assert out["api_key"] == "set" and out["ready"] and API_KEY not in json.dumps(out)
    assert call(server.podcast_list_voices())["actors"][0]["actor_id"] == "42"


def test_missing_key(monkeypatch, tmp_path):
    monkeypatch.delenv("MIRAGEA_API_KEY", raising=False)
    monkeypatch.setenv("PODCAST_OUTPUT_DIR", str(tmp_path))
    assert call(server.podcast_check_setup())["api_key"] == "missing"
    with pytest.raises(Exception, match="MIRAGEA_API_KEY"):
        call(server.podcast_list_voices())


def test_list_shows_and_get_script(engine, tmp_path):
    show, ep = make_episode(tmp_path)
    shows = call(server.podcast_list_shows())["shows"]
    assert shows[0]["episodes"] == [ep["episode_id"]]
    script = call(server.podcast_get_script(show["show_id"], ep["episode_id"]))
    assert script["lines"][0]["chapter"] == "Intro" and not script["lines"][0]["voiced"]
    with pytest.raises(Exception, match="not found"):
        call(server.podcast_get_script("nope", "x"))
