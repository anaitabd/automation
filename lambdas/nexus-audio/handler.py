"""
nexus-audio Lambda
Runtime: Python 3.12 | Memory: 2 GB | Timeout: 15 min

Generates per-sentence voiceover via ElevenLabs, applies ffmpeg audio
processing (EQ / reverb / compression / LUFS normalisation), sources
background music from Pixabay, and mixes voiceover + music + SFX.

Writes all audio assets to s3://nexus-assets/{run_id}/audio/.
"""

import json
import os
import subprocess
import tempfile
import time
import uuid
import boto3
import urllib.request
import urllib.parse

# ---------------------------------------------------------------------------
# Secrets cache
# ---------------------------------------------------------------------------
_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
S3_ASSETS_BUCKET = "nexus-assets"
S3_OUTPUTS_BUCKET = "nexus-outputs"
ELEVENLABS_MODEL = "eleven_turbo_v2_5"

# Pacing marker replacements
PACING_MAP = {
    "[PAUSE]": "...",
    "[BEAT]": ",",
    "[BREATH]": " ... ",
}

# Emotion → keyword mapping for Pixabay
MUSIC_MOOD_KEYWORDS = {
    "tension_atmospheric": "tension atmospheric",
    "corporate_upbeat_subtle": "corporate upbeat",
    "energetic_hype": "energetic upbeat",
}

# ffmpeg binary (provided via Lambda layer)
FFMPEG_BIN = "/opt/bin/ffmpeg"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _http_get(url: str, headers: dict | None = None, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _http_post_bytes(url: str, headers: dict, body: dict, retries: int = 3) -> bytes:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Pacing marker cleanup
# ---------------------------------------------------------------------------
def _clean_text(text: str) -> str:
    for marker, replacement in PACING_MAP.items():
        text = text.replace(marker, replacement)
    return text.strip()


# ---------------------------------------------------------------------------
# Emotion detection (keyword heuristics)
# ---------------------------------------------------------------------------
EMOTION_KEYWORDS = {
    "tense": ["danger", "threat", "crisis", "collapse", "war", "attack", "urgent"],
    "dramatic": ["shocking", "never before", "secret", "exposed", "revealed", "unbelievable"],
    "somber": ["tragedy", "death", "loss", "grief", "mourning", "devastation"],
    "excited": ["breakthrough", "incredible", "amazing", "launch", "success", "victory"],
    "confident": ["proven", "fact", "data shows", "research confirms", "guaranteed"],
}


def _detect_emotion(sentence: str, default_emotion: str = "neutral") -> str:
    lower = sentence.lower()
    for emotion, keywords in EMOTION_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return emotion
    return default_emotion


# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------
def _get_voice_settings(profile: dict, emotion: str) -> dict:
    voice_cfg = profile.get("voice", {})
    emotion_mapping = voice_cfg.get("emotion_mapping", {})
    if emotion in emotion_mapping:
        overrides = emotion_mapping[emotion]
        return {
            "stability": overrides.get("stability", voice_cfg.get("stability", 0.5)),
            "similarity_boost": voice_cfg.get("similarity_boost", 0.80),
            "style": overrides.get("style", voice_cfg.get("style", 0.5)),
            "use_speaker_boost": True,
        }
    return {
        "stability": voice_cfg.get("stability", 0.5),
        "similarity_boost": voice_cfg.get("similarity_boost", 0.80),
        "style": voice_cfg.get("style", 0.5),
        "use_speaker_boost": True,
    }


def _synthesize_sentence(
    text: str,
    voice_id: str,
    voice_settings: dict,
    api_key: str,
    retries: int = 3,
) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": voice_settings,
    }
    for attempt in range(retries):
        try:
            return _http_post_bytes(url, headers, body)
        except Exception:
            # If full text fails, strip pacing markers and retry
            if attempt == 0:
                body["text"] = _clean_text(text)
            elif attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _generate_voiceover(script: dict, profile: dict, api_key: str, tmpdir: str) -> str:
    """Synthesize each sentence, concatenate with 100 ms silence gaps."""
    voice_id = profile.get("voice", {}).get("voice_id", "21m00Tcm4TlvDq8ikWAM")
    segment_files: list[str] = []
    silence_path = os.path.join(tmpdir, "silence_100ms.mp3")

    # Create 100 ms silence
    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "0.1",
            "-q:a", "9", "-acodec", "libmp3lame",
            silence_path,
        ],
        check=True,
        capture_output=True,
    )

    sentences: list[str] = []
    for section in script.get("sections", []):
        content = section.get("content", "")
        default_emotion = section.get("emotion", "neutral")
        # Split on sentence boundaries
        for sent in content.replace("! ", "!|").replace("? ", "?|").replace(". ", ".|").split("|"):
            sent = sent.strip()
            if sent:
                sentences.append((sent, default_emotion))

    for idx, (sent, default_emotion) in enumerate(sentences):
        cleaned = _clean_text(sent)
        emotion = _detect_emotion(cleaned, default_emotion)
        voice_settings = _get_voice_settings(profile, emotion)
        audio_bytes = _synthesize_sentence(cleaned, voice_id, voice_settings, api_key)

        seg_path = os.path.join(tmpdir, f"seg_{idx:04d}.mp3")
        with open(seg_path, "wb") as f:
            f.write(audio_bytes)
        segment_files.append(seg_path)
        if idx < len(sentences) - 1:
            segment_files.append(silence_path)

    # Concatenate all segments
    list_file = os.path.join(tmpdir, "segments.txt")
    with open(list_file, "w") as f:
        for seg in segment_files:
            f.write(f"file '{seg}'\n")

    voiceover_raw = os.path.join(tmpdir, "voiceover_raw.mp3")
    subprocess.run(
        [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
         "-c", "copy", voiceover_raw],
        check=True,
        capture_output=True,
    )
    return voiceover_raw


# ---------------------------------------------------------------------------
# ffmpeg audio processing
# ---------------------------------------------------------------------------
def _apply_audio_processing(
    input_path: str, profile_name: str, tmpdir: str
) -> str:
    output_path = os.path.join(tmpdir, "voiceover_processed.wav")

    if profile_name == "documentary":
        # Warmth EQ + light reverb + compress + normalize -16 LUFS
        af = (
            "equalizer=f=200:width_type=o:width=2:g=3,"
            "equalizer=f=8000:width_type=o:width=2:g=-2,"
            "aecho=0.8:0.88:60:0.4,"
            "acompressor=threshold=-18dB:ratio=3:attack=5:release=50,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )
    elif profile_name == "finance":
        # Clarity EQ + compress + normalize
        af = (
            "equalizer=f=300:width_type=o:width=2:g=-3,"
            "equalizer=f=3000:width_type=o:width=2:g=3,"
            "acompressor=threshold=-18dB:ratio=4:attack=5:release=50,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )
    else:  # entertainment
        # Presence boost + hard compress + normalize
        af = (
            "equalizer=f=5000:width_type=o:width=2:g=2,"
            "equalizer=f=12000:width_type=o:width=2:g=1.5,"
            "acompressor=threshold=-18dB:ratio=8:attack=2:release=20,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )

    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", input_path, "-af", af, output_path],
        check=True,
        capture_output=True,
    )
    return output_path


# ---------------------------------------------------------------------------
# Background music (Pixabay)
# ---------------------------------------------------------------------------
def _fetch_pixabay_music(mood_keyword: str, api_key: str, tmpdir: str) -> str | None:
    query = urllib.parse.quote(MUSIC_MOOD_KEYWORDS.get(mood_keyword, mood_keyword))
    url = f"https://pixabay.com/api/videos/music/?key={api_key}&q={query}&per_page=5&category=music"
    try:
        data = json.loads(_http_get(url))
        hits = data.get("hits", [])
        if not hits:
            return None
        music_url = hits[0]["audio"]["url"]
        music_bytes = _http_get(music_url)
        music_path = os.path.join(tmpdir, "background_music.mp3")
        with open(music_path, "wb") as f:
            f.write(music_bytes)
        return music_path
    except Exception:
        return None


def _mix_audio(
    voiceover_path: str,
    music_path: str | None,
    profile: dict,
    tmpdir: str,
    run_id: str,
) -> str:
    music_vol_narration = profile.get("sound_design", {}).get("music_volume_narration", -22)
    music_vol_intro = profile.get("sound_design", {}).get("music_volume_intro", -14)
    output_path = os.path.join(tmpdir, "mixed_audio.wav")

    if music_path is None:
        # No music — just copy voiceover
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", voiceover_path, output_path],
            check=True,
            capture_output=True,
        )
        return output_path

    # Duck music: fade in 2s, fade out 3s, set volume under narration
    vol_factor_narration = 10 ** (music_vol_narration / 20)
    vol_factor_intro = 10 ** (music_vol_intro / 20)

    music_af = (
        f"afade=t=in:st=0:d=2,"
        f"volume={vol_factor_narration:.4f},"
        f"afade=t=out:d=3"
    )
    af_complex = (
        f"[1:a]{music_af}[music];"
        f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[out]"
    )

    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-i", voiceover_path,
            "-i", music_path,
            "-filter_complex", af_complex,
            "-map", "[out]",
            output_path,
        ],
        check=True,
        capture_output=True,
    )
    return output_path


# ---------------------------------------------------------------------------
# SFX injection
# ---------------------------------------------------------------------------
def _inject_sfx(
    mixed_path: str,
    script: dict,
    profile: dict,
    tmpdir: str,
    s3: "boto3.client",
) -> str:
    sfx_map = profile.get("sound_design", {}).get("sfx_map", {})
    if not sfx_map:
        return mixed_path

    sfx_inputs = []
    sfx_local: dict[str, str] = {}

    # Download required SFX from S3
    for overlay_type, s3_key in sfx_map.items():
        local_path = os.path.join(tmpdir, os.path.basename(s3_key))
        if not os.path.exists(local_path):
            try:
                s3.download_file(S3_ASSETS_BUCKET, s3_key, local_path)
            except Exception:
                continue
        sfx_local[overlay_type] = local_path

    # Determine timestamps for each SFX event (based on section overlays)
    events: list[tuple[float, str]] = []
    current_time = 5.0  # start after a short intro
    for section in script.get("sections", []):
        overlay = section.get("visual_cue", {}).get("overlay_type", "none")
        if overlay in sfx_local:
            events.append((current_time, sfx_local[overlay]))
        current_time += section.get("duration_estimate_sec", 30)

    if not events:
        return mixed_path

    sfx_out = os.path.join(tmpdir, "with_sfx.wav")
    # Build amix filter
    inputs = ["-i", mixed_path]
    filter_parts = ["[0:a]anull[base]"]
    mix_labels = ["[base]"]
    sfx_vol = 10 ** (-12 / 20)  # -12 dB relative

    for i, (ts, sfx_path) in enumerate(events):
        inputs += ["-i", sfx_path]
        label = f"[sfx{i}]"
        filter_parts.append(
            f"[{i+1}:a]adelay={int(ts*1000)}|{int(ts*1000)},volume={sfx_vol:.4f}{label}"
        )
        mix_labels.append(label)

    n = len(mix_labels)
    mix_labels_str = "".join(mix_labels)
    filter_parts.append(
        f"{mix_labels_str}amix=inputs={n}:duration=first:dropout_transition=2[out]"
    )

    subprocess.run(
        inputs + ["-filter_complex", ";".join(filter_parts), "-map", "[out]", sfx_out],
        check=True,
        capture_output=True,
    )
    return sfx_out


# ---------------------------------------------------------------------------
# S3 upload helper
# ---------------------------------------------------------------------------
def _upload_to_s3(s3, local_path: str, run_id: str, filename: str) -> str:
    key = f"{run_id}/audio/{filename}"
    s3.upload_file(local_path, S3_ASSETS_BUCKET, key)
    return key


# ---------------------------------------------------------------------------
# Error writer
# ---------------------------------------------------------------------------
def _write_error(run_id: str, step: str, exc: Exception) -> None:
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/errors/{step}.json",
            Body=json.dumps({"step": step, "error": str(exc)}).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    # Echo through for downstream states
    title: str = event.get("title", "")
    total_duration_estimate: float = float(event.get("total_duration_estimate", 0))

    try:
        s3 = boto3.client("s3")

        # Load script
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        # Load channel profile
        profile_obj = s3.get_object(Bucket="nexus-config", Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        if dry_run:
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "title": title,
                "total_duration_estimate": total_duration_estimate,
                "voiceover_s3_key": f"{run_id}/audio/voiceover_dry_run.wav",
                "mixed_audio_s3_key": f"{run_id}/audio/mixed_audio_dry_run.wav",
            }

        el_secret = get_secret("nexus/elevenlabs_api_key")
        el_api_key = el_secret["api_key"]
        pixabay_api_key = get_secret("nexus/pexels_api_key").get("pixabay_key", "")
        music_mood = profile.get("sound_design", {}).get("music_mood", "tension_atmospheric")

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Generate voiceover
            voiceover_raw = _generate_voiceover(script, profile, el_api_key, tmpdir)

            # 2. Apply audio processing
            voiceover_processed = _apply_audio_processing(
                voiceover_raw, profile_name, tmpdir
            )

            # 3. Fetch background music
            music_path = _fetch_pixabay_music(music_mood, pixabay_api_key, tmpdir)

            # 4. Mix voiceover + music
            mixed_path = _mix_audio(voiceover_processed, music_path, profile, tmpdir, run_id)

            # 5. Inject SFX
            final_audio_path = _inject_sfx(mixed_path, script, profile, tmpdir, s3)

            # 6. Upload to S3
            voiceover_key = _upload_to_s3(
                s3, voiceover_processed, run_id, "voiceover.wav"
            )
            mixed_key = _upload_to_s3(s3, final_audio_path, run_id, "mixed_audio.wav")

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "script_s3_key": script_s3_key,
            "title": title,
            "total_duration_estimate": total_duration_estimate,
            "voiceover_s3_key": voiceover_key,
            "mixed_audio_s3_key": mixed_key,
        }

    except Exception as exc:
        _write_error(run_id, "audio", exc)
        raise
