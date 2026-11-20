"""Video generation, narration, captions, and deterministic local rendering."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .ai import request_json
from .config import get_secret

RUNWAY_BASE_URL = "https://api.dev.runwayml.com/v1"
RUNWAY_API_VERSION = "2024-11-06"
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
MAX_VIDEO_BYTES = 1_000_000_000


def runway_generate_video(prompt: str, output_dir: Path, duration: int = 10) -> dict[str, Any]:
    """@brief Generate and download one vertical video with Runway."""
    api_key = get_secret("RUNWAY_API_KEY")
    if not api_key:
        raise RuntimeError("RUNWAY_API_KEY is not configured.")
    if not 4 <= duration <= 30:
        raise ValueError("Video duration must be between 4 and 30 seconds.")
    model = get_secret("RUNWAY_MODEL", "seedance2_5")
    task = request_json(
        f"{RUNWAY_BASE_URL}/text_to_video",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Runway-Version": RUNWAY_API_VERSION},
        body={"model": model, "promptText": prompt[:1000], "ratio": "720:1280", "duration": duration},
        timeout=90,
    )
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("Runway did not return a generation task ID.")
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = request_json(
            f"{RUNWAY_BASE_URL}/tasks/{urllib.parse.quote(task_id, safe='')}",
            method="GET",
            headers={"Authorization": f"Bearer {api_key}", "X-Runway-Version": RUNWAY_API_VERSION},
            timeout=45,
        )
        state = status.get("status")
        if state == "SUCCEEDED":
            outputs = status.get("output")
            if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], str):
                raise RuntimeError("Runway completed without a downloadable video URL.")
            output_path = output_dir / "source.mp4"
            download_https_file(outputs[0], output_path)
            return {"path": str(output_path), "provider": "runway", "model": model, "task_id": task_id, "estimated_cost": task.get("estimatedCost")}
        if state in {"FAILED", "CANCELED", "CANCELLED"}:
            raise RuntimeError(f"Runway generation ended with status {state}.")
        time.sleep(5)
    raise TimeoutError("Runway generation did not finish within 30 minutes; inspect provider usage before retrying.")


def download_https_file(url: str, destination: Path) -> int:
    """@brief Download a bounded HTTPS media file to a project artifact path."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Generated media must be delivered through an HTTPS URL.")
    request = urllib.request.Request(url, headers={"User-Agent": "ShortformContentStudio/0.1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f"{destination.name}.download.tmp")
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=90) as response, temporary_path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise ValueError("Generated media exceeds the 1 GB local file limit.")
                output.write(chunk)
        if total == 0:
            raise RuntimeError("The provider returned an empty media file.")
        temporary_path.replace(destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return total


def generate_voiceover(text: str, output_dir: Path) -> dict[str, Any] | None:
    """@brief Create narration and timing metadata when ElevenLabs is configured."""
    api_key = get_secret("ELEVENLABS_API_KEY")
    voice_id = get_secret("ELEVENLABS_VOICE_ID")
    if not api_key and not voice_id:
        return None
    if not api_key or not voice_id:
        raise RuntimeError("Both ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required.")
    voice = urllib.parse.quote(voice_id, safe="")
    request = urllib.request.Request(
        f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice}/with-timestamps",
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json", "xi-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"ElevenLabs request failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"ElevenLabs request failed: {type(error).__name__}.") from None
    encoded_audio = result.get("audio_base64")
    alignment = result.get("normalized_alignment") or result.get("alignment")
    if not isinstance(encoded_audio, str) or not isinstance(alignment, dict):
        raise RuntimeError("ElevenLabs response is missing audio or character timing data.")
    audio_path = output_dir / "voiceover.mp3"
    audio_path.write_bytes(base64.b64decode(encoded_audio, validate=True))
    captions_path = output_dir / "captions.srt"
    captions_path.write_text(alignment_to_srt(alignment), encoding="utf-8")
    return {"audio_path": str(audio_path), "captions_path": str(captions_path), "provider": "elevenlabs", "voice_id": voice_id}


def alignment_to_srt(alignment: dict[str, Any], max_chars: int = 26) -> str:
    """@brief Convert character-level speech alignment into readable SRT cues."""
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not isinstance(chars, list) or not isinstance(starts, list) or not isinstance(ends, list) or not (len(chars) == len(starts) == len(ends)):
        raise ValueError("Speech alignment arrays must have equal lengths.")
    cues: list[tuple[float, float, str]] = []
    buffer = ""
    cue_start: float | None = None
    cue_end = 0.0
    for index, char in enumerate(chars):
        if not isinstance(char, str) or not char:
            continue
        try:
            start = float(starts[index])
            end = float(ends[index])
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        if cue_start is None:
            cue_start = start
        buffer += char
        cue_end = end
        if len(buffer) >= max_chars or char in {".", "?", "!", "。", "？", "！", ",", "，", "\n"}:
            clean = buffer.strip()
            if clean:
                cues.append((cue_start, max(cue_end, cue_start + 0.2), clean))
            buffer = ""
            cue_start = None
    if buffer.strip() and cue_start is not None:
        cues.append((cue_start, max(cue_end, cue_start + 0.2), buffer.strip()))
    if not cues:
        raise ValueError("Speech alignment did not contain any usable captions.")
    lines: list[str] = []
    for index, (start, end, text) in enumerate(cues, 1):
        lines.extend((str(index), f"{_srt_time(start)} --> {_srt_time(end)}", text, ""))
    return "\n".join(lines)


def _srt_time(seconds: float) -> str:
    """@brief Format non-negative seconds as an SRT timestamp."""
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def render_video(source_path: Path, output_path: Path, *, audio_path: Path | None = None, captions_path: Path | None = None) -> Path:
    """@brief Render an MP4 and optionally loop visuals under narration captions."""
    ffmpeg = shutil.which("ffmpeg")
    staging_path = output_path.with_name(f"{output_path.stem}.rendering{output_path.suffix}")
    if not ffmpeg:
        if audio_path or captions_path:
            raise RuntimeError("FFmpeg is required to combine narration and captions; install it and add ffmpeg.exe to PATH.")
        try:
            shutil.copy2(source_path, staging_path)
            staging_path.replace(output_path)
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
        return output_path
    command = [ffmpeg, "-y"]
    if audio_path:
        # Looping the visual lets the full narration finish instead of cutting audio to one generated shot.
        command.extend(["-stream_loop", "-1", "-i", source_path.name, "-i", audio_path.name])
    else:
        command.extend(["-i", source_path.name])
    filters: list[str] = []
    if captions_path:
        filters.append("subtitles=captions.srt:force_style='FontName=Malgun Gothic,FontSize=42,Outline=3,Shadow=1,Alignment=2,MarginV=170'")
    if filters:
        command.extend(["-vf", ",".join(filters)])
    if audio_path:
        command.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
    elif captions_path:
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy"])
    else:
        command.extend(["-c", "copy"])
    command.extend([staging_path.name])
    try:
        result = subprocess.run(command, cwd=source_path.parent, capture_output=True, text=True, timeout=1800, check=False)
        if result.returncode != 0:
            detail = result.stderr[-1200:].strip()
            raise RuntimeError(f"FFmpeg render failed with exit code {result.returncode}: {detail}")
        staging_path.replace(output_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return output_path


def probe_video(video_path: Path) -> dict[str, Any]:
    """@brief Read media streams and dimensions with ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"available": False, "passed": False, "message": "ffprobe is not installed; media properties were not verified."}
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(video_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return {"available": True, "passed": False, "message": "ffprobe could not parse the video file."}
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"available": True, "passed": False, "message": "ffprobe returned malformed metadata."}
    streams = metadata.get("streams")
    format_info = metadata.get("format")
    if not isinstance(streams, list) or not isinstance(format_info, dict):
        return {"available": True, "passed": False, "message": "ffprobe returned incomplete media metadata."}
    video_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), None)
    try:
        duration = float(format_info.get("duration") or 0)
        width = int(video_stream.get("width") or 0) if video_stream else 0
        height = int(video_stream.get("height") or 0) if video_stream else 0
    except (TypeError, ValueError):
        return {"available": True, "passed": False, "message": "ffprobe returned invalid media dimensions or duration."}
    checks = [bool(video_stream), height > width, 0 < duration <= 180, width >= 480, height >= 640]
    return {
        "available": True,
        "passed": all(checks),
        "has_video": bool(video_stream),
        "has_audio": bool(audio_stream),
        "width": width,
        "height": height,
        "duration_seconds": round(duration, 2),
        "message": "Vertical format, duration, and minimum dimensions checked; human review is still required.",
    }
