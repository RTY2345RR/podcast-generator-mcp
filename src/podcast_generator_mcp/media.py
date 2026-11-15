"""ffmpeg helpers: decode host lines, stitch the episode, encode MP3 with ID3 chapters.

ffmpeg comes from the system PATH when installed, otherwise from the bundled
`imageio-ffmpeg` wheel, so `uvx podcast-generator-mcp` works with no extra setup.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

SAMPLE_RATE = 44_100  # podcast standard
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


class MediaError(Exception):
    pass


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    env = os.environ.get("FFMPEG_BINARY", "").strip()
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-untyped]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the platform wheel
        raise MediaError(
            "ffmpeg was not found. Install it (macOS: brew install ffmpeg, Ubuntu: apt install ffmpeg, "
            "Windows: winget install ffmpeg) or set FFMPEG_BINARY."
        ) from exc


def run_ffmpeg(args: list[str], *, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise MediaError("ffmpeg failed: " + " | ".join(tail))
    return proc


def probe_duration(path: Path) -> float:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True)
    m = DURATION_RE.search(proc.stderr.decode("utf-8", "replace"))
    if not m:
        raise MediaError(f"Could not read the duration of {path}.")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def decode_line(src: Path, rate: int = SAMPLE_RATE) -> bytes:
    """Decode one synthesized line to mono s16le PCM with leading/trailing silence trimmed."""
    trim = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05"
    tail = "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.08,areverse"
    fade = "afade=t=in:d=0.01"
    args = ["-i", str(src), "-vn", "-ac", "1", "-ar", str(rate), "-af", f"{trim},{tail},{fade}",
            "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    return run_ffmpeg(args).stdout


def pcm_seconds(pcm: bytes, rate: int = SAMPLE_RATE) -> float:
    return len(pcm) / 2 / rate


class WavWriter:
    """Stream a mono 16-bit WAV: silence and PCM are appended in order (memory stays small)."""

    def __init__(self, path: Path, rate: int = SAMPLE_RATE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.rate = rate
        self._w = wave.open(str(path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(rate)
        self.frames = 0

    @property
    def seconds(self) -> float:
        return self.frames / self.rate

    def silence(self, seconds: float) -> None:
        frames = int(round(max(0.0, seconds) * self.rate))
        while frames > 0:
            n = min(48_000, frames)
            self._w.writeframes(b"\x00\x00" * n)
            self.frames += n
            frames -= n

    def pcm(self, data: bytes) -> None:
        if len(data) % 2:
            data = data[:-1]
        self._w.writeframes(data)
        self.frames += len(data) // 2

    def close(self) -> None:
        self._w.close()


def _ffmeta_escape(text: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def ffmetadata(tags: dict[str, str], chapters: Sequence[tuple[float, float, str]]) -> str:
    """FFMETADATA1 text: global tags plus chapters (written to MP3 as ID3 CHAP/CTOC frames)."""
    lines = [";FFMETADATA1"]
    for k, v in tags.items():
        if v:
            lines.append(f"{k}={_ffmeta_escape(str(v))}")
    for start, end, title in chapters:
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(start * 1000))}",
                  f"END={int(round(end * 1000))}", f"title={_ffmeta_escape(title)}"]
    return "\n".join(lines) + "\n"


def encode_episode(
    wav: Path,
    dest: Path,
    *,
    metadata_file: Optional[Path] = None,
    bitrate: str = "128k",
    loudness: Optional[float] = -16.0,
) -> Path:
    """Encode the stitched WAV to MP3 (mono, 44.1 kHz), loudness-normalized to podcast level (-16 LUFS)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(wav)]
    if metadata_file:
        args += ["-i", str(metadata_file), "-map_metadata", "1", "-map_chapters", "1"]
    args += ["-map", "0:a"]
    if loudness is not None:
        args += ["-af", f"loudnorm=I={loudness}:TP=-1.5:LRA=11"]
    args += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "libmp3lame", "-b:a", bitrate,
             "-id3v2_version", "3", "-write_xing", "1", str(dest)]
    run_ffmpeg(args)
    return dest
