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
ELEVENLABS_MODEL = "eleven_multilingual_v2"

# Resets to False on every cold start — never persisted externally.
ELEVENLABS_QUOTA_EXHAUSTED = False

POLLY_VOICE_MAP = {
    "documentary": "Gregory",
    "finance": "Matthew",
    "entertainment": "Stephen",
    "true_crime": "Gregory",
}

SSML_EMOTION_MAP = {
    "tense":         {"rate": "slow",   "pitch": "-2st"},
    "excited":       {"rate": "fast",   "pitch": "+3st"},
    "reflective":    {"rate": "x-slow", "pitch": "-3st"},
    "authoritative": {"rate": "medium", "pitch": "-1st"},
    "somber":        {"rate": "slow",   "pitch": "-4st"},
    "hopeful":       {"rate": "medium", "pitch": "+1st"},
    "neutral":       {"rate": "medium", "pitch": "0st"},
    # True Crime emotion extensions
    "whispering":    {"rate": "x-slow", "pitch": "-5st"},
    "urgent":        {"rate": "fast",   "pitch": "+1st"},
    "revelation":    {"rate": "medium", "pitch": "-1st"},
    "dark":          {"rate": "slow",   "pitch": "-3st"},
    "suspenseful":   {"rate": "slow",   "pitch": "-2st"},
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


def _elevenlabs_tts_once(url: str, headers: dict, body: dict) -> bytes:
    """Call ElevenLabs exactly once with an 8-second timeout."""
    data = json.dumps(body).encode("utf-8")
    merged = {"User-Agent": "NexusCloud/1.0"}
    merged.update(headers)
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read()


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
    return (
        "quota_exceeded" in body_str
        or "credits_used" in body_str
        or "limit_reached" in body_str
    )


def _get_polly_voice_id(profile: dict) -> str:
    voice_cfg = profile.get("voice", {})
    return (
        profile.get("polly_voice_id")
        or voice_cfg.get("polly_voice_id")
        or os.environ.get("POLLY_VOICE_ID")
        or POLLY_VOICE_MAP.get(profile.get("name", ""), "Matthew")
    )


def _apply_punctuation_pauses(text: str) -> str:
    """Convert punctuation to SSML break tags for True Crime pacing."""
    text = text.replace("...", '<break time="700ms"/>')
    text = text.replace(" — ", '<break time="400ms"/>')
    text = text.replace(" - ", '<break time="300ms"/>')
    return text


def _build_ssml(text: str, emotion: str) -> str:
    mapping = SSML_EMOTION_MAP.get(emotion, SSML_EMOTION_MAP["neutral"])
    rate = mapping["rate"]
    pitch = mapping["pitch"]
    text_with_pauses = _apply_punctuation_pauses(text)
    return (
        f'<speak>'
        f'<prosody rate="{rate}" pitch="{pitch}">'
        f'<amazon:effect name="drc">'
        f'<amazon:breath duration="short" volume="soft"/>'
        f'{text_with_pauses}'
        f'</amazon:effect>'
        f'</prosody>'
        f'</speak>'
    )


def detect_emotion(sentence: str) -> str:
    """Detect the appropriate True Crime emotion for a sentence.

    Rules are checked in priority order. Always returns a key that exists
    in SSML_EMOTION_MAP — no KeyError is possible.
    """
    s = sentence
    lower = s.lower()

    if s.startswith((
        "No one knew", "She never", "The last thing",
        "What they found", "Nobody expected", "He never came",
    )):
        return "whispering"

    if any(kw in lower for kw in (
        "suddenly", "within hours", "police discovered",
        "the call came in", "emergency", "immediately",
    )):
        return "urgent"

    if any(kw in lower for kw in (
        "turned out", "it was", "they later confirmed",
        "the truth was", "forensics revealed", "dna showed",
    )):
        return "revelation"

    if s.endswith("?"):
        return "suspenseful"

    if any(kw in lower for kw in (
        "body", "victim", "disappeared", "never seen again",
        "remains", "evidence suggested",
    )):
        return "dark"

    if any(kw in lower for kw in (
        "family", "mother", "daughter", "son", "father",
        "remembered", "loved ones",
    )):
        return "somber"

    return "tense"


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
        LanguageCode="en-US",
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


def _make_silence_mp3(tmpdir: str, duration_sec: float, label: str) -> str:
    """Generate a silent MP3 file of the given duration."""
    path = os.path.join(tmpdir, f"silence_{label}.mp3")
    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(duration_sec),
            "-q:a", "9", "-acodec", "libmp3lame",
            path,
        ],
        check=True, capture_output=True,
    )
    return path


def generate_voiceover(
    narration: str,
    short_id: str,
    profile: dict,
    target_duration: float,
    tmpdir: str,
    run_id: str = "",
    emotion: str = "neutral",
) -> tuple[str, dict | None]:
    """Generate a voiceover WAV file for a short-form narration.

    Uses the 3-tier TTS cascade: ElevenLabs → Polly Neural → Polly Standard.
    Returns (local WAV path, cached word timestamps or None).
    """
    global ELEVENLABS_QUOTA_EXHAUSTED

    cached_timestamps = None
    if run_id:
        cached_timestamps = _load_cached_timestamps(run_id)
        if cached_timestamps is not None:
            log.info("[%s] word_timestamps.json found — reusing cached timestamps", run_id)

    # Resolve emotion: validate against SSML_EMOTION_MAP
    if emotion not in SSML_EMOTION_MAP:
        emotion = detect_emotion(narration)

    is_true_crime = profile.get("script", {}).get("style") == "true_crime"

    audio_bytes: bytes | None = None

    # Tier 1: ElevenLabs — try once, timeout=8s
    if not ELEVENLABS_QUOTA_EXHAUSTED:
        try:
            el_secret = get_secret("nexus/elevenlabs_api_key")
            api_key = el_secret.get("api_key", "")
            voice_id = profile.get("voice", {}).get("voice_id", "")
            if api_key and voice_id:
                url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
                headers = {
                    "xi-api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                }
                voice_cfg = profile.get("voice", {})
                body = {
                    "text": narration,
                    "model_id": ELEVENLABS_MODEL,
                    "voice_settings": {
                        "stability": voice_cfg.get("stability", 0.35),
                        "similarity_boost": voice_cfg.get("similarity_boost", 0.75),
                        "style": voice_cfg.get("style", 0.45),
                    },
                }
                audio_bytes = _elevenlabs_tts_once(url, headers, body)
        except Exception as exc:
            if _should_fallback_to_polly(exc) or isinstance(exc, (TimeoutError, OSError)):
                ELEVENLABS_QUOTA_EXHAUSTED = True
                log.warning("[%s] tts: ElevenLabs → Polly Neural. Reason: %s", run_id, exc)
            else:
                log.warning("[%s] ElevenLabs unexpected error: %s — falling back to Polly Neural", run_id, exc)
                ELEVENLABS_QUOTA_EXHAUSTED = True
    else:
        log.info("[%s] tts: quota exhausted, using Polly Neural directly", run_id)

    # Tier 2: Polly Neural
    if audio_bytes is None:
        try:
            audio_bytes = _synthesize_with_polly_neural(narration, emotion, profile)
        except Exception as exc:
            log.warning("[%s] Polly Neural failed (%s) — falling back to Polly Standard", run_id, exc)
            # Tier 3: Polly Standard
            audio_bytes = _synthesize_with_polly_standard(narration, profile)

    mp3_path = os.path.join(tmpdir, f"vo_{short_id}.mp3")
    with open(mp3_path, "wb") as f:
        f.write(audio_bytes)

    # Append 0.8s dramatic silence after revelation/whispering scenes
    silence_emotions = ("revelation", "whispering")
    if emotion in silence_emotions:
        silence_path = _make_silence_mp3(tmpdir, 0.8, f"drama_{short_id}")
        combined_path = os.path.join(tmpdir, f"vo_{short_id}_with_silence.mp3")
        list_file = os.path.join(tmpdir, f"vo_{short_id}_list.txt")
        with open(list_file, "w") as f:
            f.write(f"file '{mp3_path}'\n")
            f.write(f"file '{silence_path}'\n")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", combined_path],
            check=True, capture_output=True,
        )
        mp3_path = combined_path

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

