"""ElevenLabs TTS for short-form voiceovers."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request

import boto3

from config import FFMPEG_BIN, SCRATCH_DIR

_cache: dict = {}

log = logging.getLogger(__name__)

S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")

POLLY_VOICE_MAP = {
    "documentary": "Gregory",
    "finance": "Matthew",
    "entertainment": "Stephen",
}

SSML_EMOTION_MAP = {
    "tense":         {"rate": "slow",   "pitch": "-2st"},
    "excited":       {"rate": "fast",   "pitch": "+3st"},
    "reflective":    {"rate": "x-slow", "pitch": "-3st"},
    "authoritative": {"rate": "medium", "pitch": "-1st"},
    "somber":        {"rate": "slow",   "pitch": "-4st"},
    "hopeful":       {"rate": "medium", "pitch": "+1st"},
    "neutral":       {"rate": "medium", "pitch": "0st"},
}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def _load_cached_timestamps(run_id: str) -> dict | None:
    s3 = boto3.client("s3")
    key = f"{run_id}/audio/word_timestamps.json"
    try:
        obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


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
    if status_code in {401, 429}:
        return True
    body_str = json.dumps(payload).lower() if isinstance(payload, dict) else str(payload).lower()
    return "quota_exceeded" in body_str or "credits_used" in body_str


def _get_polly_voice_id(profile: dict) -> str:
    voice_cfg = profile.get("voice", {})
    return (
        voice_cfg.get("polly_voice_id")
        or os.environ.get("POLLY_VOICE_ID")
        or POLLY_VOICE_MAP.get(profile.get("name", ""), "Matthew")
    )


def _build_ssml(text: str, emotion: str) -> str:
    mapping = SSML_EMOTION_MAP.get(emotion, SSML_EMOTION_MAP["neutral"])
    rate = mapping["rate"]
    pitch = mapping["pitch"]
    return (
        f'<speak><prosody rate="{rate}" pitch="{pitch}">'
        f'<amazon:effect name="drc">{text}</amazon:effect>'
        f'</prosody></speak>'
    )


def _synthesize_with_polly_neural(narration: str, emotion: str, profile: dict) -> bytes:
    polly = boto3.client("polly")
    voice_id = _get_polly_voice_id(profile)
    ssml_text = _build_ssml(narration, emotion)
    response = polly.synthesize_speech(
        Engine="neural",
        VoiceId=voice_id,
        OutputFormat="mp3",
        SampleRate="24000",
        Text=ssml_text,
        TextType="ssml",
    )
    return response["AudioStream"].read()


def _synthesize_with_polly_standard(narration: str, profile: dict) -> bytes:
    polly = boto3.client("polly")
    voice_id = _get_polly_voice_id(profile)
    response = polly.synthesize_speech(
        Engine="standard",
        VoiceId=voice_id,
        OutputFormat="mp3",
        SampleRate="22050",
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
    run_id: str = "",
) -> tuple[str, dict | None]:
    """Generate a voiceover WAV file for a short-form narration.

    Uses voice settings from the profile (not hardcoded).
    Returns (local WAV path, cached word timestamps or None).
    """
    cached_timestamps = None
    if run_id:
        cached_timestamps = _load_cached_timestamps(run_id)
        if cached_timestamps is not None:
            log.info("[%s] word_timestamps.json found — reusing cached timestamps", run_id)

    # ElevenLabs disabled — go directly to Polly Neural (Tier 2)
    log.info("[%s] TTS: using Polly Neural (ElevenLabs disabled)", run_id)
    try:
        audio_bytes = _synthesize_with_polly_neural(narration, "neutral", profile)
    except Exception as exc:
        log.warning("[%s] Polly Neural failed (%s) — falling back to Polly Standard", run_id, exc)
        audio_bytes = _synthesize_with_polly_standard(narration, profile)

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
        return sped_path, cached_timestamps

    return wav_path, cached_timestamps


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

