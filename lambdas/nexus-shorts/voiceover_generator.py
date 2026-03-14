"""ElevenLabs TTS for short-form voiceovers."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import boto3

from config import FFMPEG_BIN, SCRATCH_DIR

_cache: dict = {}

POLLY_VOICE_MAP = {
    "documentary": "Matthew",
    "finance": "Matthew",
    "entertainment": "Joanna",
}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def _http_post_bytes(url: str, headers: dict, body: dict, retries: int = 3) -> bytes:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            merged = {"User-Agent": "NexusCloud/1.0"}
            merged.update(headers)
            req = urllib.request.Request(url, data=data, headers=merged, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (3 ** attempt))  # 5 / 15 / 45s
    raise RuntimeError("Unreachable")


def _extract_tts_error(exc: Exception) -> tuple[int | None, dict]:
    status_code = getattr(exc, "code", None)
    payload: dict = {}
    if isinstance(exc, urllib.error.HTTPError):
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                payload = {"raw": raw.decode("utf-8", errors="replace")}
    return status_code, payload


def _should_fallback_to_polly(exc: Exception) -> bool:
    status_code, payload = _extract_tts_error(exc)
    if status_code in {401, 402, 403, 429}:
        return True

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        status = str(detail.get("status", "")).lower()
        message = str(detail.get("message", "")).lower()
        return any(
            token in f"{status} {message}"
            for token in ("quota_exceeded", "unauthorized", "voice_not", "forbidden")
        )

    return False


def _get_polly_voice_id(profile: dict) -> str:
    voice_cfg = profile.get("voice", {})
    return (
        voice_cfg.get("polly_voice_id")
        or os.environ.get("POLLY_VOICE_ID")
        or POLLY_VOICE_MAP.get(profile.get("name", ""), "Matthew")
    )


def _synthesize_with_polly(narration: str, profile: dict) -> bytes:
    polly = boto3.client("polly")
    response = polly.synthesize_speech(
        Engine="neural",
        VoiceId=_get_polly_voice_id(profile),
        OutputFormat="mp3",
        SampleRate="24000",
        Text=narration,
        TextType="text",
    )
    return response["AudioStream"].read()


def generate_voiceover(
    narration: str,
    short_id: str,
    profile: dict,
    target_duration: float,
    tmpdir: str,
) -> str:
    """Generate a voiceover WAV file for a short-form narration.

    Uses voice settings from the profile (not hardcoded).
    Returns the local path to the output WAV.
    """
    el_secret = get_secret("nexus/elevenlabs_api_key")
    api_key = el_secret["api_key"]

    voice_cfg = profile.get("voice", {})
    voice_id = voice_cfg.get("voice_id")
    if not voice_id:
        raise ValueError("Profile missing voice.voice_id — check profile JSON in CONFIG_BUCKET")
    model_id = voice_cfg.get("model_id", "eleven_multilingual_v2")

    # Use shorts-specific voice settings if available, else fallback to main
    shorts_cfg = profile.get("shorts", {})
    stability = shorts_cfg.get("voice_stability", voice_cfg.get("stability", 0.5))
    similarity = shorts_cfg.get("voice_similarity_boost", voice_cfg.get("similarity_boost", 0.75))
    style = shorts_cfg.get("voice_style", voice_cfg.get("style", 0.3))

    voice_settings = {
        "stability": stability,
        "similarity_boost": similarity,
        "style": style,
        "use_speaker_boost": True,
    }

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": narration,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }

    try:
        audio_bytes = _http_post_bytes(url, headers, body)
    except Exception as exc:
        if not _should_fallback_to_polly(exc):
            raise
        audio_bytes = _synthesize_with_polly(narration, profile)

    mp3_path = os.path.join(tmpdir, f"vo_{short_id}.mp3")
    with open(mp3_path, "wb") as f:
        f.write(audio_bytes)

    # Convert to WAV for processing
    wav_path = os.path.join(tmpdir, f"vo_{short_id}.wav")
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", mp3_path, "-ar", "44100", "-ac", "1", wav_path],
        check=True, capture_output=True,
    )

    # Speed-adjust if voiceover is longer than target duration (leave 1s buffer)
    vo_dur = _get_duration(wav_path)
    max_dur = target_duration - 1.0  # leave 0.5s intro + 0.5s outro
    if vo_dur > max_dur and max_dur > 0:
        speed_factor = min(1.2, vo_dur / max_dur)
        sped_path = os.path.join(tmpdir, f"vo_{short_id}_sped.wav")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", wav_path,
             "-af", f"atempo={speed_factor:.3f}",
             sped_path],
            check=True, capture_output=True,
        )
        return sped_path

    return wav_path


def _get_duration(path: str) -> float:
    try:
        from config import FFPROBE_BIN
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0

