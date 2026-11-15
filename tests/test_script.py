import pytest

from podcast_generator_mcp.exports import build_srt, chapters_text, clock, split_cues
from podcast_generator_mcp.show import ShowError, clean_host, parse_script

from .conftest import HOSTS

HS = [clean_host(h) for h in HOSTS]


def test_parse_text_script_with_chapters_tags_and_continuations():
    script = """
## Intro
Maya: Welcome to the show!
**Leo:** Thanks, Maya.
It is great to be here.
[chapter: The news]
[maya]: (laughs) So, what happened this week?
- Leo: A lot.
"""
    lines = parse_script(script, HS)
    assert [ln["host"] for ln in lines] == ["Maya", "Leo", "Maya", "Leo"]
    assert lines[0]["chapter"] == "Intro" and lines[2]["chapter"] == "The news"
    assert lines[1]["text"] == "Thanks, Maya. It is great to be here."
    assert lines[2]["tags"] == ["laughs"] and lines[2]["text"] == "So, what happened this week?"


def test_parse_list_script():
    lines = parse_script([{"host": "leo", "text": "Hi.", "pause_after": 9, "chapter": "Start"}], HS)
    assert lines == [{"host": "Leo", "text": "Hi.", "chapter": "Start", "pause_after": 5.0}]


@pytest.mark.parametrize("bad", ["Sam: who am I?", "no host here", "", [{"host": "Maya", "text": ""}], 42])
def test_parse_errors(bad):
    with pytest.raises(ShowError):
        parse_script(bad, HS)


def test_unknown_single_word_host_mid_script_is_an_error():
    with pytest.raises(ShowError, match="unknown host 'Sam'"):
        parse_script("Maya: hi\nSam: hello", HS)


def test_clean_host_rules():
    with pytest.raises(ShowError, match="exactly one"):
        clean_host({"name": "A"})
    with pytest.raises(ShowError, match="Hugging Face"):
        clean_host({"name": "A", "reference_audio_url": "https://huggingface.co/x/resolve/main/a.wav"})
    with pytest.raises(ShowError, match="speed"):
        clean_host({"name": "A", "actor_id": "1", "speed": 9})
    assert clean_host({"name": "A", "actor_id": 7}) == {"name": "A", "actor_id": "7"}


def test_split_cues_and_srt():
    long = "This is a sentence. " + "word " * 40
    cues = split_cues(long)
    assert all(len(c) <= 84 for c in cues)
    srt = build_srt([{"host": "Maya", "text": "Hello there. How are you?", "start": 0, "end": 2.0},
                     {"host": "Leo", "text": "Fine.", "start": 2.4, "end": 3.0}])
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "Maya: Hello there. How are you?" in srt and "Leo: Fine." in srt
    assert "00:00:02,400 --> 00:00:03,000" in srt


def test_chapter_text_and_clock():
    assert clock(65) == "01:05" and clock(3725) == "1:02:05"
    assert chapters_text([{"start": 0, "title": "Intro"}, {"start": 61.2, "title": "News"}]) == "00:00 Intro\n01:01 News\n"
