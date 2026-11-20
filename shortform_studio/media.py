"""Video generation, narration, captions, and deterministic local rendering."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
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
_FONT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9가-힣 _-]{1,64}$")
_ASS_COLOR_PATTERN = re.compile(r"^&H[0-9A-Fa-f]{8}$")
_SRT_TIME_PATTERN = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})$")

SUBTITLE_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "clean_bottom": {
        "label": "깔끔한 하단 자막",
        "font_name": "",
        "font_size": 46,
        "margin_v": 200,
        "outline": 4,
        "shadow": 2,
        "alignment": 2,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
    },
    "shorts_punch": {
        "label": "쇼츠 볼드 강조 자막",
        "font_name": "",
        "font_size": 54,
        "margin_v": 240,
        "outline": 5,
        "shadow": 2,
        "alignment": 2,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
    },
    "bold_center": {
        "label": "강조 중앙 자막",
        "font_name": "",
        "font_size": 52,
        "margin_v": 80,
        "outline": 4,
        "shadow": 2,
        "alignment": 5,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
    },
    "yellow_pop": {
        "label": "노란색 포인트 자막",
        "font_name": "",
        "font_size": 46,
        "margin_v": 210,
        "outline": 4,
        "shadow": 2,
        "alignment": 2,
        "primary_colour": "&H0000E5FF",
        "outline_colour": "&H00000000",
    },
    "top_news": {
        "label": "상단 뉴스형 자막",
        "font_name": "",
        "font_size": 40,
        "margin_v": 180,
        "outline": 3,
        "shadow": 1,
        "alignment": 8,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
    },
}


def _contains_hangul(text: str) -> bool:
    """@brief Detect whether text contains Korean Hangul syllables or Jamo characters."""
    return bool(re.search(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]", str(text or "")))


def default_subtitle_font() -> str:
    """@brief Choose a readable default system font name for burned-in subtitles."""
    import sys

    if sys.platform == "win32":
        return "Malgun Gothic"
    if sys.platform == "darwin":
        return "Apple SD Gothic Neo"
    return "Noto Sans CJK KR"


def normalize_subtitle_style(style: dict[str, Any] | None = None) -> dict[str, Any]:
    """@brief Validate subtitle style parameters and merge with a named preset."""
    raw = style if isinstance(style, dict) else {}
    preset_id = str(raw.get("preset") or "clean_bottom").strip()
    if preset_id not in SUBTITLE_STYLE_PRESETS:
        raise ValueError("Unsupported subtitle preset.")
    merged = dict(SUBTITLE_STYLE_PRESETS[preset_id])
    merged["preset"] = preset_id

    font_name = str(raw.get("font_name") if raw.get("font_name") is not None else merged["font_name"]).strip()
    if not font_name:
        font_name = default_subtitle_font()
    if not _FONT_NAME_PATTERN.fullmatch(font_name):
        raise ValueError("Subtitle font name contains unsupported characters.")
    merged["font_name"] = font_name

    def _bounded_int(key: str, minimum: int, maximum: int) -> int:
        value = raw.get(key, merged[key])
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"Subtitle {key} must be an integer between {minimum} and {maximum}.")
        return value

    merged["font_size"] = _bounded_int("font_size", 18, 96)
    merged["margin_v"] = _bounded_int("margin_v", 10, 600)
    merged["outline"] = _bounded_int("outline", 0, 10)
    merged["shadow"] = _bounded_int("shadow", 0, 6)
    alignment = raw.get("alignment", merged["alignment"])
    if alignment not in {2, 5, 8}:
        raise ValueError("Subtitle alignment must be bottom (2), center (5), or top (8).")
    merged["alignment"] = alignment

    for color_key in ("primary_colour", "outline_colour"):
        color_val = str(raw.get(color_key) or merged[color_key]).strip()
        if not _ASS_COLOR_PATTERN.fullmatch(color_val):
            raise ValueError(f"Subtitle {color_key} must use ASS &HAABBGGRR format.")
        merged[color_key] = color_val.upper()
    return merged


def _srt_timestamp_ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    """@brief Convert parsed SRT timestamp groups to non-negative milliseconds."""
    h, m, s, ms = int(hours), int(minutes), int(seconds), int(millis)
    if m >= 60 or s >= 60:
        raise ValueError("Invalid SRT timestamp minutes or seconds.")
    return ((h * 3600) + (m * 60) + s) * 1000 + ms


def validate_and_normalize_srt(raw_srt: str) -> str:
    """@brief Parse, validate, and renumber SRT subtitle cues."""
    if not isinstance(raw_srt, str) or not raw_srt.strip():
        raise ValueError("Subtitle SRT content cannot be empty.")
    if len(raw_srt) > 200_000:
        raise ValueError("Subtitle SRT content exceeds the 200,000-character limit.")
    normalized_text = raw_srt.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n{2,}", normalized_text)
    if len(blocks) > 1000:
        raise ValueError("Subtitle SRT exceeds the 1,000-cue limit.")
    cues: list[str] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip() != ""]
        if len(lines) < 2:
            raise ValueError("Each SRT cue must include a timestamp range and text.")
        time_line_index = 1 if lines[0].strip().isdigit() else 0
        if time_line_index >= len(lines):
            raise ValueError("Missing SRT timestamp line.")
        match = _SRT_TIME_PATTERN.fullmatch(lines[time_line_index].strip())
        if not match:
            raise ValueError(f"Invalid SRT timestamp line: {lines[time_line_index][:40]}")
        start_ms = _srt_timestamp_ms(*match.groups()[:4])
        end_ms = _srt_timestamp_ms(*match.groups()[4:])
        if end_ms <= start_ms:
            raise ValueError("Each SRT cue end time must be greater than its start time.")
        cue_lines = lines[time_line_index + 1 :]
        if not cue_lines:
            raise ValueError("Each SRT cue must contain subtitle text.")
        cues.append(
            "\n".join(
                [
                    str(len(cues) + 1),
                    f"{_srt_time(start_ms / 1000.0)} --> {_srt_time(end_ms / 1000.0)}",
                    *cue_lines,
                ]
            )
        )
    if not cues:
        raise ValueError("Subtitle SRT must contain at least one valid cue.")
    return "\n\n".join(cues) + "\n"


def build_subtitles_filter(captions_filename: str, style: dict[str, Any] | None = None) -> str:
    """@brief Build a safe FFmpeg subtitles filter expression with ASS force_style."""
    normalized = normalize_subtitle_style(style)
    safe_filename = captions_filename.replace("\\", "/").replace("'", "\\'").replace(":", "\\:")
    force_style = (
        f"FontName={normalized['font_name']},"
        f"FontSize={normalized['font_size']},"
        f"Outline={normalized['outline']},"
        f"Shadow={normalized['shadow']},"
        f"Alignment={normalized['alignment']},"
        f"MarginV={normalized['margin_v']},"
        f"PrimaryColour={normalized['primary_colour']},"
        f"OutlineColour={normalized['outline_colour']}"
    )
    return f"subtitles={safe_filename}:force_style='{force_style}'"


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


def generate_voiceover(
    text: str,
    output_dir: Path,
    *,
    tts_provider: str = "auto",
    voice: str = "",
) -> dict[str, Any] | None:
    """@brief Create narration and timing metadata using ElevenLabs or OpenAI TTS."""
    provider = str(tts_provider or "auto").strip().lower()
    if provider == "none":
        return None
    if provider == "auto":
        if get_secret("ELEVENLABS_API_KEY") and get_secret("ELEVENLABS_VOICE_ID"):
            provider = "elevenlabs"
        elif get_secret("OPENAI_API_KEY"):
            provider = "openai_tts"
        elif get_secret("ELEVENLABS_API_KEY") or get_secret("ELEVENLABS_VOICE_ID"):
            raise RuntimeError("Both ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required.")
        else:
            return None

    if provider == "openai_tts":
        return _generate_openai_tts(text, output_dir, voice=voice)
    if provider != "elevenlabs":
        raise ValueError("Unsupported TTS provider; choose auto, elevenlabs, openai_tts, or none.")

    api_key = get_secret("ELEVENLABS_API_KEY")
    voice_id = voice.strip() or get_secret("ELEVENLABS_VOICE_ID")
    if not api_key and not voice_id:
        return None
    if not api_key or not voice_id:
        raise RuntimeError("Both ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required.")
    encoded_voice = urllib.parse.quote(voice_id, safe="")
    model_id = get_secret("ELEVENLABS_MODEL", "eleven_multilingual_v2") or "eleven_multilingual_v2"
    request = urllib.request.Request(
        f"{ELEVENLABS_BASE_URL}/text-to-speech/{encoded_voice}/with-timestamps",
        data=json.dumps({"text": text, "model_id": model_id}).encode("utf-8"),
        headers={"Content-Type": "application/json", "xi-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            err_body = json.loads(error.read(4096).decode("utf-8", errors="replace"))
            err_detail = err_body.get("detail") if isinstance(err_body, dict) else None
            if isinstance(err_detail, dict) and isinstance(err_detail.get("message"), str):
                detail = f": {err_detail['message'][:300]}"
            elif isinstance(err_detail, str):
                detail = f": {err_detail[:300]}"
        except Exception:
            detail = ""
        raise RuntimeError(f"ElevenLabs request failed with HTTP {error.code}{detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"ElevenLabs request failed: {type(error).__name__}.") from None
    encoded_audio = result.get("audio_base64")
    raw_alignment = result.get("alignment")
    norm_alignment = result.get("normalized_alignment")
    has_hangul = _contains_hangul(text)
    # ElevenLabs normalized_alignment romanizes Hangul into Latin phonetics ("annyeonghaseyo");
    # always prefer raw alignment (or source text timings) so Korean subtitles stay in Hangul.
    alignment = raw_alignment if (has_hangul and isinstance(raw_alignment, dict)) else (norm_alignment or raw_alignment)
    if not isinstance(encoded_audio, str) or not isinstance(alignment, dict):
        raise RuntimeError("ElevenLabs response is missing audio or character timing data.")
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "voiceover.mp3"
    audio_path.write_bytes(base64.b64decode(encoded_audio, validate=True))
    captions_path = output_dir / "captions.srt"
    aligned_chars = "".join(str(ch) for ch in (alignment.get("characters") or []) if isinstance(ch, str))
    if has_hangul and not _contains_hangul(aligned_chars):
        duration = probe_audio_duration(audio_path)
        if duration is None or duration <= 0:
            from .planning import estimate_speaking_duration

            duration = max(2.0, estimate_speaking_duration(text)["estimated_seconds"])
        srt_text = text_to_fallback_srt(text, duration, max_chars=16)
    else:
        srt_text = alignment_to_srt(alignment, max_chars=16 if has_hangul else 24)
    captions_path.write_text(srt_text, encoding="utf-8")
    return {"audio_path": str(audio_path), "captions_path": str(captions_path), "provider": "elevenlabs", "voice_id": voice_id}


def _generate_openai_tts(text: str, output_dir: Path, *, voice: str = "") -> dict[str, Any]:
    """@brief Synthesize MP3 narration with OpenAI TTS and produce proportional SRT captions."""
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI TTS.")
    chosen_voice = voice.strip() or get_secret("OPENAI_TTS_VOICE", "alloy")
    if chosen_voice not in {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"}:
        chosen_voice = "alloy"
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps({"model": "tts-1", "voice": chosen_voice, "input": text[:4096], "response_format": "mp3"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            audio_bytes = response.read(50_000_000)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"OpenAI TTS request failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"OpenAI TTS request failed: {type(error).__name__}.") from None
    if not audio_bytes:
        raise RuntimeError("OpenAI TTS returned empty audio.")
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "voiceover.mp3"
    audio_path.write_bytes(audio_bytes)
    duration = probe_audio_duration(audio_path)
    if duration is None or duration <= 0:
        from .planning import estimate_speaking_duration

        duration = max(2.0, estimate_speaking_duration(text)["estimated_seconds"])
    captions_path = output_dir / "captions.srt"
    captions_path.write_text(text_to_fallback_srt(text, duration, max_chars=16 if _contains_hangul(text) else 26), encoding="utf-8")
    return {"audio_path": str(audio_path), "captions_path": str(captions_path), "provider": "openai_tts", "voice_id": chosen_voice}


def text_to_fallback_srt(text: str, total_seconds: float, max_chars: int = 26) -> str:
    """@brief Split spoken text proportionally across a known duration into SRT cues."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        raise ValueError("Cannot build SRT cues from empty text.")
    duration = max(0.5, float(total_seconds))
    raw_segments = [seg.strip() for seg in re.split(r"(?<=[.!?。？！,])\s+", cleaned) if seg.strip()]
    segments: list[str] = []
    for seg in raw_segments:
        if len(seg) <= max_chars:
            segments.append(seg)
            continue
        words = seg.split(" ")
        buf = ""
        for word in words:
            candidate = f"{buf} {word}".strip() if buf else word
            if len(candidate) > max_chars and buf:
                segments.append(buf)
                buf = word
            else:
                buf = candidate
        if buf:
            segments.append(buf)
    if not segments:
        segments = [cleaned]
    weights = [max(1, len(seg)) for seg in segments]
    total_weight = sum(weights)
    cursor = 0.0
    lines: list[str] = []
    for idx, (seg, weight) in enumerate(zip(segments, weights), 1):
        span = duration * (weight / total_weight)
        end = duration if idx == len(segments) else min(duration, cursor + max(0.25, span))
        lines.extend((str(idx), f"{_srt_time(cursor)} --> {_srt_time(max(end, cursor + 0.2))}", seg, ""))
        cursor = end
    return "\n".join(lines)


def probe_audio_duration(audio_path: Path) -> float | None:
    """@brief Read audio duration in seconds with ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not audio_path.is_file():
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    raw_stdout = getattr(result, "stdout", None)
    if not isinstance(raw_stdout, str) or not raw_stdout.strip():
        return None
    try:
        payload = json.loads(raw_stdout)
        duration = float((payload.get("format") or {}).get("duration") or 0)
        return duration if duration > 0 else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


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


def _build_audio_mix_graph(
    voice_idx: int | None,
    bgm_idx: int | None,
    *,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
) -> tuple[list[str], str | None]:
    """@brief Build FFmpeg filter_complex audio nodes for narration, ducked BGM, and loudness normalization."""
    filters: list[str] = []
    vol = max(0.02, min(1.0, float(bgm_volume)))
    if voice_idx is not None and bgm_idx is not None:
        filters.append(f"[{bgm_idx}:a:0]volume={vol:.2f}[bgm_scaled]")
        if ducking:
            filters.append(f"[{voice_idx}:a:0]asplit=2[voice_sc][voice_mix]")
            filters.append("[bgm_scaled][voice_sc]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=250[bgm_ducked]")
            filters.append("[voice_mix][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[a_mixed]")
        else:
            filters.append(f"[{voice_idx}:a:0][bgm_scaled]amix=inputs=2:duration=first:dropout_transition=2[a_mixed]")
        out_label = "[a_mixed]"
    elif voice_idx is not None:
        out_label = f"[{voice_idx}:a:0]"
    elif bgm_idx is not None:
        filters.append(f"[{bgm_idx}:a:0]volume={vol:.2f}[bgm_only]")
        out_label = "[bgm_only]"
    else:
        return [], None
    if loudnorm:
        filters.append(f"{out_label}loudnorm=I=-14:TP=-1.5:LRA=11[a_norm]")
        out_label = "[a_norm]"
    return filters, out_label


def _run_ffmpeg(
    command: list[str],
    *,
    cwd: Path,
    cancel_check: Any = None,
    timeout: int = 900,
) -> tuple[int, str]:
    """@brief Execute FFmpeg with non-blocking cancellation polling and bounded stderr capture."""
    if cancel_check is None:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (getattr(result, "stderr", "") or "")[-1200:].strip()
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile("w+t", encoding="utf-8", errors="replace") as err_log:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=err_log)
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    err_log.flush()
                    err_log.seek(0)
                    return code, err_log.read()[-1200:].strip()
                if cancel_check():
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                    raise RuntimeError("Operation cancelled by operator.")
                if time.monotonic() >= deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                    raise RuntimeError("FFmpeg timed out while rendering video.")
                time.sleep(0.35)
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            raise


def render_video(
    source_path: Path,
    output_path: Path,
    *,
    audio_path: Path | None = None,
    captions_path: Path | None = None,
    subtitle_style: dict[str, Any] | None = None,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
    cancel_check: Any = None,
) -> Path:
    """@brief Render an MP4 and optionally combine narration, BGM ducking, and burned-in captions."""
    ffmpeg = shutil.which("ffmpeg")
    staging_path = output_path.with_name(f"{output_path.stem}.rendering{output_path.suffix}")
    if not ffmpeg:
        if audio_path or captions_path or bgm_path or loudnorm:
            raise RuntimeError("FFmpeg is required to combine narration, BGM, and captions; install it and add ffmpeg.exe to PATH.")
        try:
            shutil.copy2(source_path, staging_path)
            staging_path.replace(output_path)
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
        return output_path
    command = [ffmpeg, "-y"]
    audio_duration: float | None = None
    if audio_path:
        # Looping the visual lets the full narration finish instead of cutting audio to one generated shot.
        audio_duration = probe_audio_duration(audio_path)
        command.extend(["-stream_loop", "-1", "-i", source_path.name, "-i", audio_path.name])
    else:
        command.extend(["-i", source_path.name])
    if bgm_path:
        command.extend(["-stream_loop", "-1", "-i", bgm_path.name])

    voice_idx = 1 if audio_path else None
    bgm_idx = (2 if audio_path else 1) if bgm_path else None
    duration_args = ["-t", f"{audio_duration + 0.10:.2f}"] if (audio_duration is not None and audio_duration > 0) else []
    use_complex = bool(bgm_path or (loudnorm and audio_path))
    if use_complex:
        filter_parts: list[str] = []
        v_label = "[0:v:0]"
        if captions_path:
            filter_parts.append(f"[0:v:0]{build_subtitles_filter(captions_path.name, subtitle_style)}[v_out]")
            v_label = "[v_out]"
        audio_parts, a_label = _build_audio_mix_graph(voice_idx, bgm_idx, bgm_volume=bgm_volume, ducking=ducking, loudnorm=loudnorm)
        filter_parts.extend(audio_parts)
        command.extend(["-filter_complex", ";".join(filter_parts), "-map", v_label])
        if a_label:
            command.extend(["-map", a_label, "-shortest", *duration_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
        else:
            command.extend([*duration_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"])
    else:
        filters: list[str] = []
        if captions_path:
            filters.append(build_subtitles_filter(captions_path.name, subtitle_style))
        if filters:
            command.extend(["-vf", ",".join(filters)])
        if audio_path:
            command.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest", *duration_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
        elif captions_path:
            command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"])
        else:
            command.extend(["-c", "copy"])
    command.extend([staging_path.name])
    try:
        returncode, detail = _run_ffmpeg(command, cwd=source_path.parent, cancel_check=cancel_check)
        if returncode != 0:
            raise RuntimeError(f"FFmpeg render failed with exit code {returncode}: {detail}")
        staging_path.replace(output_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return output_path


def build_multiscene_ffmpeg_command(
    ffmpeg_bin: str,
    clips: list[dict[str, Any]],
    output_name: str,
    *,
    width: int = 1080,
    height: int = 1920,
    transition: str = "cut",
    transition_seconds: float = 0.35,
    audio_name: str | None = None,
    captions_name: str | None = None,
    subtitle_style: dict[str, Any] | None = None,
    bgm_name: str | None = None,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
) -> list[str]:
    """@brief Construct a deterministic FFmpeg filter_complex command to assemble multiple 9:16 scenes."""
    if not clips or len(clips) > 8:
        raise ValueError("Multi-scene assembly requires between 1 and 8 scene clips.")
    if transition not in {"cut", "fade"}:
        raise ValueError("Transition must be 'cut' or 'fade'.")
    command = [ffmpeg_bin, "-y"]
    filter_parts: list[str] = []
    durations: list[float] = []
    for idx, clip in enumerate(clips):
        dur = max(1.0, min(30.0, float(clip.get("duration_seconds") or 5.0)))
        durations.append(dur)
        rel_path = str(clip["relative_path"]).replace("\\", "/")
        if clip.get("media_type") == "image":
            command.extend(["-loop", "1", "-t", f"{dur:.2f}", "-i", rel_path])
            frames = max(30, int(dur * 30))
            filter_parts.append(
                f"[{idx}:v:0]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},zoompan=z='min(zoom+0.0008,1.08)':d={frames}:s={width}x{height}:fps=30,"
                f"setsar=1,format=yuv420p[v{idx}]"
            )
        else:
            command.extend(["-stream_loop", "-1", "-t", f"{dur:.2f}", "-i", rel_path])
            filter_parts.append(
                f"[{idx}:v:0]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30,format=yuv420p[v{idx}]"
            )

    if len(clips) == 1:
        video_label = "[v0]"
    elif transition == "fade":
        fade = max(0.1, min(1.0, float(transition_seconds)))
        current_label = "[v0]"
        timeline = durations[0]
        for idx in range(1, len(clips)):
            offset = max(0.1, timeline - fade)
            next_label = f"[vx{idx}]"
            filter_parts.append(f"{current_label}[v{idx}]xfade=transition=fade:duration={fade:.2f}:offset={offset:.2f}{next_label}")
            current_label = next_label
            timeline = offset + durations[idx]
        video_label = current_label
    else:
        inputs = "".join(f"[v{idx}]" for idx in range(len(clips)))
        filter_parts.append(f"{inputs}concat=n={len(clips)}:v=1:a=0[v_cat]")
        video_label = "[v_cat]"

    if captions_name:
        filter_parts.append(f"{video_label}{build_subtitles_filter(captions_name, subtitle_style)}[v_sub]")
        video_label = "[v_sub]"

    next_input = len(clips)
    voice_idx: int | None = None
    bgm_idx: int | None = None
    if audio_name:
        command.extend(["-i", audio_name])
        voice_idx = next_input
        next_input += 1
    if bgm_name:
        command.extend(["-stream_loop", "-1", "-i", bgm_name])
        bgm_idx = next_input

    audio_parts, audio_label = _build_audio_mix_graph(
        voice_idx,
        bgm_idx,
        bgm_volume=bgm_volume,
        ducking=ducking,
        loudnorm=loudnorm,
    )
    filter_parts.extend(audio_parts)
    command.extend(["-filter_complex", ";".join(filter_parts), "-map", video_label])
    if audio_label:
        command.extend(["-map", audio_label, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
    else:
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart"])
    command.append(output_name)
    return command


def assemble_multiscene_video(
    work_dir: Path,
    clips: list[dict[str, Any]],
    output_path: Path,
    *,
    transition: str = "cut",
    audio_path: Path | None = None,
    captions_path: Path | None = None,
    subtitle_style: dict[str, Any] | None = None,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
    cancel_check: Any = None,
) -> Path:
    """@brief Execute FFmpeg multi-scene assembly into a vertical 9:16 MP4."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for multi-scene assembly; install it and add ffmpeg to PATH.")
    staging_path = output_path.with_name(f"{output_path.stem}.assembling{output_path.suffix}")
    command = build_multiscene_ffmpeg_command(
        ffmpeg,
        clips,
        staging_path.name,
        transition=transition,
        audio_name=audio_path.name if audio_path else None,
        captions_name=captions_path.name if captions_path else None,
        subtitle_style=subtitle_style,
        bgm_name=bgm_path.name if bgm_path else None,
        bgm_volume=bgm_volume,
        ducking=ducking,
        loudnorm=loudnorm,
    )
    try:
        returncode, detail = _run_ffmpeg(command, cwd=work_dir, cancel_check=cancel_check)
        if returncode != 0:
            raise RuntimeError(f"FFmpeg multi-scene assembly failed with exit code {returncode}: {detail}")
        staging_path.replace(output_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return output_path


def extract_video_frame(video_path: Path, output_path: Path, timestamp_seconds: float = 0.5) -> Path:
    """@brief Extract one JPEG thumbnail frame from an MP4 using FFmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to extract a thumbnail frame from video.")
    if not video_path.is_file():
        raise FileNotFoundError("Source MP4 file was not found.")
    ts = max(0.0, float(timestamp_seconds))
    staging_path = output_path.with_name(f"{output_path.stem}.frame{output_path.suffix}")
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{ts:.2f}",
        "-i",
        video_path.name,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        staging_path.name,
    ]
    try:
        result = subprocess.run(command, cwd=video_path.parent, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0 or not staging_path.is_file():
            detail = result.stderr[-800:].strip()
            raise RuntimeError(f"FFmpeg thumbnail extraction failed: {detail}")
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
