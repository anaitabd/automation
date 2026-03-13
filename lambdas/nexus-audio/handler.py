import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import urllib.request
import urllib.parse
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-audio")

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")
ELEVENLABS_MODEL = "eleven_multilingual_v2"

s3 = boto3.client("s3")

PACING_MAP = {
    "[PAUSE]": "...",
    "[BEAT]": ",",
    "[BREATH]": " ... ",
}

MUSIC_MOOD_KEYWORDS = {
    "tension_atmospheric": "tension atmospheric",
    "corporate_upbeat_subtle": "corporate upbeat",
    "energetic_hype": "energetic upbeat",
}

def _find_ffmpeg() -> str:
    """Locate the ffmpeg binary.

    Search order:
      1. /opt/bin/ffmpeg  – AWS Lambda layer path
      2. 'ffmpeg' on $PATH – Docker / local dev
    Raises FileNotFoundError if ffmpeg cannot be found anywhere.
    """
    for candidate in ("/opt/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Fall back to bare name (relies on $PATH)
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise FileNotFoundError(
        "ffmpeg not found. Install ffmpeg or set the FFMPEG_BIN env var."
    )


FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or _find_ffmpeg()


def _http_get(url: str, headers: dict | None = None, retries: int = 3) -> bytes:
    merged = {"User-Agent": "NexusCloud/1.0"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
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
            merged = {"User-Agent": "NexusCloud/1.0"}
            merged.update(headers)
            req = urllib.request.Request(url, data=data, headers=merged, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _clean_text(text: str) -> str:
    for marker, replacement in PACING_MAP.items():
        text = text.replace(marker, replacement)
    return text.strip()


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


def _get_voice_settings(profile: dict, emotion: str) -> dict:
    voice_cfg = profile.get("voice", {})
    emotion_mapping = voice_cfg.get("emotion_mapping", {})
    if emotion in emotion_mapping:
        overrides = emotion_mapping[emotion]
        return {
            "stability": overrides.get("stability", voice_cfg.get("stability", 0.35)),
            "similarity_boost": voice_cfg.get("similarity_boost", 0.75),
            "style": overrides.get("style", voice_cfg.get("style", 0.45)),
            "use_speaker_boost": True,
        }
    return {
        "stability": voice_cfg.get("stability", 0.35),
        "similarity_boost": voice_cfg.get("similarity_boost", 0.75),
        "style": voice_cfg.get("style", 0.45),
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
            if attempt == 0:
                body["text"] = _clean_text(text)
            elif attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _generate_voiceover(script: dict, profile: dict, api_key: str, tmpdir: str) -> str:
    voice_id = profile.get("voice", {}).get("voice_id", "21m00Tcm4TlvDq8ikWAM")
    segment_files: list[str] = []

    def _make_silence(duration_ms: int, label: str) -> str:
        path = os.path.join(tmpdir, f"silence_{label}.mp3")
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", str(duration_ms / 1000),
                "-q:a", "9", "-acodec", "libmp3lame",
                path,
            ],
            check=True,
            capture_output=True,
        )
        return path

    silence_300ms = _make_silence(300, "300ms")
    silence_600ms = _make_silence(600, "600ms")

    sentences: list[tuple[str, str, str]] = []
    for section in script.get("sections", []):
        content = section.get("content", "")
        default_emotion = section.get("emotion", "neutral")
        parts = content.replace("! ", "!\x00").replace("? ", "?\x00").replace(". ", ".\x00").split("\x00")
        for sent in parts:
            sent = sent.strip()
            if not sent:
                continue
            sentences.append((sent, default_emotion, "600ms"))

    TTS_WORKERS = int(os.environ.get("TTS_PARALLELISM", "5"))

    def _synth_one(idx: int, sent: str, default_emotion: str):
        cleaned = _clean_text(sent)
        emotion = _detect_emotion(cleaned, default_emotion)
        voice_settings = _get_voice_settings(profile, emotion)
        audio_bytes = _synthesize_sentence(cleaned, voice_id, voice_settings, api_key)
        seg_path = os.path.join(tmpdir, f"seg_{idx:04d}.mp3")
        with open(seg_path, "wb") as f:
            f.write(audio_bytes)
        return idx, seg_path

    seg_map: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=TTS_WORKERS) as pool:
        futures = {
            pool.submit(_synth_one, idx, sent, emo): idx
            for idx, (sent, emo, _silence) in enumerate(sentences)
        }
        for fut in as_completed(futures):
            idx, seg_path = fut.result()
            seg_map[idx] = seg_path

    for idx, (_sent, _emo, silence_label) in enumerate(sentences):
        segment_files.append(seg_map[idx])
        if idx < len(sentences) - 1:
            segment_files.append(silence_300ms if silence_label == "300ms" else silence_600ms)

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


def _apply_audio_processing(
    input_path: str, profile_name: str, tmpdir: str
) -> str:
    output_path = os.path.join(tmpdir, "voiceover_processed.wav")

    if profile_name == "documentary":
        af = (
            "equalizer=f=200:width_type=o:width=2:g=3,"
            "equalizer=f=3000:width_type=o:width=2:g=1.5,"
            "equalizer=f=8000:width_type=o:width=2:g=-2,"
            "acompressor=threshold=-18dB:ratio=3:attack=5:release=80,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )
    elif profile_name == "finance":
        af = (
            "equalizer=f=300:width_type=o:width=2:g=-3,"
            "equalizer=f=3000:width_type=o:width=2:g=3,"
            "acompressor=threshold=-18dB:ratio=4:attack=5:release=50,"
            "loudnorm=I=-16:TP=-1.5:LRA=11"
        )
    else:
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


def _fetch_pixabay_music(mood_keyword: str, api_key: str, tmpdir: str) -> str | None:
    if not api_key:
        log.warning("No Pixabay API key — skipping background music")
        return None
    query = urllib.parse.quote(MUSIC_MOOD_KEYWORDS.get(mood_keyword, mood_keyword))
    # Pixabay Music API endpoint — separate from image API
    url = f"https://pixabay.com/api/music/?key={api_key}&q={query}&per_page=5"
    try:
        data = json.loads(_http_get(url))
        hits = data.get("hits", [])
        if not hits:
            log.info("Pixabay music API returned 0 results for query=%r", query)
            return None
        # Music API returns audio field with quality levels, or a direct audio URL
        hit = hits[0]
        audio_field = hit.get("audio") or {}
        music_url = (
            (audio_field.get("128") or audio_field.get("64") or audio_field.get("32"))
            if isinstance(audio_field, dict) else None
        ) or hit.get("previewURL") or hit.get("url", "")
        if not music_url or not str(music_url).startswith("http"):
            log.warning("Pixabay music hit has no usable audio URL: %s", hit.keys())
            return None
        music_bytes = _http_get(music_url)
        if len(music_bytes) < 5000:
            log.warning("Music download suspiciously small (%d bytes) — skipping", len(music_bytes))
            return None
        music_path = os.path.join(tmpdir, "background_music.mp3")
        with open(music_path, "wb") as f:
            f.write(music_bytes)
        log.info("Downloaded background music (%.1f KB)", len(music_bytes) / 1024)
        return music_path
    except Exception as exc:
        log.warning("Pixabay music fetch failed: %s", exc)
        return None


def _mix_audio(
    voiceover_path: str,
    music_path: str | None,
    profile: dict,
    tmpdir: str,
    run_id: str,
) -> str:
    # Music at -18dB relative to voice reference (0dB); profile override still respected if set
    music_vol_narration = profile.get("sound_design", {}).get("music_volume_narration", -18)
    output_path = os.path.join(tmpdir, "mixed_audio.wav")

    if music_path is None:
        # Apply dynaudnorm on voice-only path too for consistent loudness
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", voiceover_path,
             "-af", "dynaudnorm=p=0.9:m=100",
             output_path],
            check=True,
            capture_output=True,
        )
        return output_path

    vol_factor_narration = 10 ** (music_vol_narration / 20)

    # Loop music to cover the full voiceover duration (handles short preview clips)
    looped_music = os.path.join(tmpdir, "background_music_looped.wav")
    try:
        # Get voiceover duration so we know how long to loop music
        probe = subprocess.run(
            [FFMPEG_BIN.replace("ffmpeg", "ffprobe") if os.path.isfile(FFMPEG_BIN.replace("ffmpeg", "ffprobe")) else "ffprobe",
             "-v", "quiet", "-print_format", "json", "-show_format", voiceover_path],
            capture_output=True, check=True,
        )
        vo_duration = float(json.loads(probe.stdout).get("format", {}).get("duration", 600))
        subprocess.run(
            [FFMPEG_BIN, "-y", "-stream_loop", "-1", "-i", music_path,
             "-filter_complex", "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[music_fmt]",
             "-map", "[music_fmt]",
             "-t", str(vo_duration + 10),
             looped_music],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("Music loop failed (stderr=%s), using original music", exc.stderr[-500:] if exc.stderr else "")
        looped_music = music_path  # fallback to original
    except Exception as exc:
        log.warning("Music loop failed: %s — using original music", exc)
        looped_music = music_path

    # Music chain: fade in, set -18dB volume, fade out
    music_af = (
        f"afade=t=in:st=0:d=2,"
        f"volume={vol_factor_narration:.4f},"
        f"afade=t=out:d=3"
    )

    # Try with sidechain compression first; fall back to simple mix if unsupported
    def _run_complex_mix() -> None:
        # dynaudnorm on voice track before mixing (voice=0dB reference)
        af_complex = (
            f"[0:a]dynaudnorm=p=0.9:m=100,asplit=2[sc][vo];"
            f"[1:a]{music_af}[music_raw];"
            f"[sc][music_raw]sidechaincompress=threshold=0.02:ratio=4:attack=200:release=1000[ducked];"
            f"[vo][ducked]amix=inputs=2:duration=first:dropout_transition=3[out]"
        )
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", voiceover_path, "-i", looped_music,
             "-filter_complex", af_complex, "-map", "[out]", output_path],
            check=True,
            capture_output=True,
        )

    def _run_simple_mix() -> None:
        # dynaudnorm on voice track before mixing (voice=0dB reference)
        af_complex = (
            f"[0:a]dynaudnorm=p=0.9:m=100[vo_norm];"
            f"[1:a]{music_af}[music_raw];"
            f"[vo_norm][music_raw]amix=inputs=2:duration=first:dropout_transition=3[out]"
        )
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", voiceover_path, "-i", looped_music,
             "-filter_complex", af_complex, "-map", "[out]", output_path],
            check=True,
            capture_output=True,
        )

    try:
        _run_complex_mix()
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or b"")[-600:].decode("utf-8", errors="replace")
        log.warning("sidechaincompress mix failed (exit=%d, stderr=%s); retrying with simple mix",
                    exc.returncode, stderr_tail)
        try:
            _run_simple_mix()
        except subprocess.CalledProcessError as exc2:
            stderr_tail2 = (exc2.stderr or b"")[-600:].decode("utf-8", errors="replace")
            log.warning("Simple mix failed (exit=%d, stderr=%s); using voiceover only",
                        exc2.returncode, stderr_tail2)
            subprocess.run(
                [FFMPEG_BIN, "-y", "-i", voiceover_path,
                 "-af", "dynaudnorm=p=0.9:m=100",
                 output_path],
                check=True, capture_output=True,
            )

    return output_path


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

    for overlay_type, s3_key in sfx_map.items():
        local_path = os.path.join(tmpdir, os.path.basename(s3_key))
        if not os.path.exists(local_path):
            try:
                s3.download_file(S3_ASSETS_BUCKET, s3_key, local_path)
            except Exception:
                continue
        sfx_local[overlay_type] = local_path

    events: list[tuple[float, str]] = []
    current_time = 5.0
    for section in script.get("sections", []):
        overlay = section.get("visual_cue", {}).get("overlay_type", "none")
        if overlay in sfx_local:
            events.append((current_time, sfx_local[overlay]))
        current_time += section.get("duration_estimate_sec", 30)

    if not events:
        return mixed_path

    sfx_out = os.path.join(tmpdir, "with_sfx.wav")
    inputs = ["-i", mixed_path]
    filter_parts = ["[0:a]anull[base]"]
    mix_labels = ["[base]"]
    sfx_vol = 10 ** (-12 / 20)

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
        [FFMPEG_BIN, "-y"] + inputs + ["-filter_complex", ";".join(filter_parts), "-map", "[out]", sfx_out],
        check=True,
        capture_output=True,
    )
    return sfx_out


def _upload_to_s3(s3, local_path: str, run_id: str, filename: str) -> str:
    key = f"{run_id}/audio/{filename}"
    s3.upload_file(local_path, S3_ASSETS_BUCKET, key)
    return key


def _write_error(run_id: str, step: str, exc: Exception) -> None:
    try:
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/errors/{step}.json",
            Body=json.dumps({"step": step, "error": str(exc)}).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass


SCRATCH_DIR = os.environ.get("TMPDIR", "/mnt/scratch")


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event.get("run_id") or os.environ.get("RUN_ID", "")
    profile_name: str = event.get("profile") or os.environ.get("PROFILE", "documentary")
    niche: str = event.get("niche") or os.environ.get("NICHE", "")
    script_s3_key: str = event.get("script_s3_key") or os.environ.get("SCRIPT_S3_KEY", "")
    dry_run_raw = event.get("dry_run") if "dry_run" in event else os.environ.get("DRY_RUN", "false")
    dry_run: bool = dry_run_raw if isinstance(dry_run_raw, bool) else str(dry_run_raw).lower() == "true"
    title: str = event.get("title") or os.environ.get("TITLE", "")
    total_duration_estimate: float = float(event.get("total_duration_estimate") or os.environ.get("TOTAL_DURATION_ESTIMATE", 0))

    step_start = notify_step_start("audio", run_id, niche=niche, profile=profile_name, dry_run=dry_run)

    try:
        log.info("Loading script from S3: %s", script_s3_key)
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        log.info("Loading profile: %s", profile_name)
        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        if dry_run:
            log.info("DRY RUN mode — returning stub audio keys")
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

        log.info("Fetching ElevenLabs API key")
        el_secret = get_secret("nexus/elevenlabs_api_key")
        el_api_key = el_secret["api_key"]
        pixabay_api_key = get_secret("nexus/pexels_api_key").get("pixabay_key", "")
        music_mood = profile.get("sound_design", {}).get("music_mood", "tension_atmospheric")

        with tempfile.TemporaryDirectory(dir=SCRATCH_DIR if os.path.isdir(SCRATCH_DIR) else None) as tmpdir:
            log.info("Generating voiceover via ElevenLabs (%d sections)", len(script.get("sections", [])))
            voiceover_raw = _generate_voiceover(script, profile, el_api_key, tmpdir)

            log.info("Applying audio processing")
            voiceover_processed = _apply_audio_processing(
                voiceover_raw, profile_name, tmpdir
            )

            log.info("Fetching background music (mood=%s)", music_mood)
            music_path = _fetch_pixabay_music(music_mood, pixabay_api_key, tmpdir)

            log.info("Mixing audio tracks")
            mixed_path = _mix_audio(voiceover_processed, music_path, profile, tmpdir, run_id)

            log.info("Injecting SFX")
            final_audio_path = _inject_sfx(mixed_path, script, profile, tmpdir, s3)

            log.info("Uploading audio files to S3")
            voiceover_key = _upload_to_s3(
                s3, voiceover_processed, run_id, "voiceover.wav"
            )
            mixed_key = _upload_to_s3(s3, final_audio_path, run_id, "mixed_audio.wav")

        elapsed = time.time() - step_start
        notify_step_complete("audio", run_id, [
            {"name": "Title", "value": title[:100], "inline": False},
            {"name": "Voiceover", "value": voiceover_key.split("/")[-1], "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0xE67E22)

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
        log.error("Audio step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "audio", exc)
        raise
    finally:
        import shutil
        scratch_dir = os.path.join(SCRATCH_DIR, run_id)
        shutil.rmtree(scratch_dir, ignore_errors=True)


if __name__ == "__main__":
    import sys
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
    sys.exit(0)
if __name__ == "__main__":
    import sys
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
    sys.exit(0)