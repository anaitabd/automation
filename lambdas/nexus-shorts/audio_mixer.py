"""Voiceover + music mixing and mastering to -14 LUFS."""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import urllib.request
import time

from config import (
    FFMPEG_BIN, FFPROBE_BIN, OUTPUT_AUDIO_BITRATE,
    S3_ASSETS_BUCKET, TARGET_LUFS, TRUE_PEAK_LIMIT,
)

logger = logging.getLogger(__name__)

_cache: dict = {}

# Music ducking level under voiceover (dB). True Crime uses more aggressive -18dB.
_DEFAULT_MUSIC_VOLUME = 0.12   # ~-18dB relative
_TRUE_CRIME_MUSIC_VOLUME = 0.06  # ~-24dB (more aggressive ducking under VO)


def get_secret(name: str) -> dict:
    if name not in _cache:
        import boto3
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def _is_true_crime(profile: dict) -> bool:
    """Return True if this is a true_crime profile."""
    return profile.get("script", {}).get("style", "") == "true_crime"


def _apply_sfx_accents(
    mixed_path: str,
    cut_points: list[float],
    tmpdir: str,
    short_id: str,
) -> str:
    """Add subtle SFX accent (low-frequency whoosh) at each cut point.

    Returns path to the final audio file with SFX mixed in.
    Falls back to mixed_path unchanged on any error.
    """
    if not cut_points:
        return mixed_path

    try:
        # Generate a short whoosh SFX
        whoosh_path = os.path.join(tmpdir, f"whoosh_{short_id}.wav")
        subprocess.run(
            [FFMPEG_BIN, "-y",
             "-f", "lavfi", "-i", "sine=frequency=80:duration=0.4",
             "-af", "afade=t=in:st=0:d=0.05,afade=t=out:st=0.3:d=0.1,volume=0.3",
             whoosh_path],
            check=True, capture_output=True,
        )

        # Build adelay filters for each cut point
        sfx_inputs = []
        filter_parts = []
        for i, t in enumerate(cut_points[:8]):  # cap at 8 SFX accents
            delay_ms = int(t * 1000)
            sfx_inputs.extend(["-i", whoosh_path])
            filter_parts.append(f"[{i + 1}:a]adelay={delay_ms}|{delay_ms}[sfx{i}]")

        n_sfx = len(cut_points[:8])
        mix_labels = "[0:a]" + "".join(f"[sfx{i}]" for i in range(n_sfx))
        filter_parts.append(
            f"{mix_labels}amix=inputs={n_sfx + 1}:normalize=0[sfx_out]"
        )

        sfx_mixed = os.path.join(tmpdir, f"sfx_mixed_{short_id}.wav")
        cmd = [FFMPEG_BIN, "-y", "-i", mixed_path] + sfx_inputs + [
            "-filter_complex", ";".join(filter_parts),
            "-map", "[sfx_out]",
            sfx_mixed,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return sfx_mixed

    except Exception as exc:
        logger.warning("[%s] SFX accent overlay failed (non-fatal): %s", short_id, exc)
        return mixed_path


def mix_audio(
    voiceover_path: str,
    music_path: str | None,
    target_duration: float,
    short_id: str,
    tmpdir: str,
    profile: dict | None = None,
    cut_points: list[float] | None = None,
) -> str:
    """Mix voiceover + background music and master to -14 LUFS.

    For true_crime profiles:
    - Music ducked more aggressively (-18dB under VO, volume=0.06)
    - SFX accents applied at cut points if profile["audio"]["sfx_enabled"] is True

    Mastering chain:
    1. High-pass filter at 80Hz (remove rumble)
    2. Compression: -18dB threshold, 3:1 ratio, 10ms attack
    3. Air boost EQ: +2.5dB shelf at 12kHz
    4. Loudness normalization: -14 LUFS
    5. True peak limiter: -1dBTP
    """
    profile = profile or {}
    output_path = os.path.join(tmpdir, f"mixed_{short_id}.wav")

    tc = _is_true_crime(profile)
    music_volume = _TRUE_CRIME_MUSIC_VOLUME if tc else _DEFAULT_MUSIC_VOLUME

    if music_path and os.path.isfile(music_path):
        # Mix VO + music with ducking
        # VO starts at 0.5s (brief music intro)
        mixed_raw = os.path.join(tmpdir, f"mix_raw_{short_id}.wav")

        # Loop music to cover full duration
        looped_music = os.path.join(tmpdir, f"music_loop_{short_id}.wav")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-stream_loop", "-1", "-i", music_path,
             "-t", str(target_duration + 2),
             "-af", "aformat=sample_rates=44100:channel_layouts=stereo",
             looped_music],
            check=True, capture_output=True,
        )

        # Mix with sidechain-style ducking
        af_complex = (
            f"[0:a]adelay=500|500,aformat=sample_rates=44100:channel_layouts=stereo,"
            f"asplit=2[sc][vo];"
            f"[1:a]afade=t=in:st=0:d=1,volume={music_volume},"
            f"afade=t=out:st={target_duration - 1}:d=1[music];"
            f"[sc][music]sidechaincompress=threshold=0.03:ratio=4:attack=200:release=800[ducked];"
            f"[vo][ducked]amix=inputs=2:duration=first:dropout_transition=2[out]"
        )

        try:
            subprocess.run(
                [FFMPEG_BIN, "-y", "-i", voiceover_path, "-i", looped_music,
                 "-filter_complex", af_complex, "-map", "[out]",
                 "-t", str(target_duration), mixed_raw],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            # Fallback: simple volume mix
            af_simple = (
                f"[0:a]adelay=500|500[vo];"
                f"[1:a]volume={music_volume}[music];"
                f"[vo][music]amix=inputs=2:duration=first[out]"
            )
            subprocess.run(
                [FFMPEG_BIN, "-y", "-i", voiceover_path, "-i", looped_music,
                 "-filter_complex", af_simple, "-map", "[out]",
                 "-t", str(target_duration), mixed_raw],
                check=True, capture_output=True,
            )
    else:
        # Voiceover only — pad with silence for 0.5s intro
        mixed_raw = os.path.join(tmpdir, f"mix_raw_{short_id}.wav")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", voiceover_path,
             "-af", f"adelay=500|500,apad=whole_dur={target_duration}",
             "-t", str(target_duration), mixed_raw],
            check=True, capture_output=True,
        )

    # SFX accents at cut points (true_crime only when sfx_enabled)
    sfx_enabled = profile.get("audio", {}).get("sfx_enabled", False)
    if tc and sfx_enabled and cut_points:
        mixed_raw = _apply_sfx_accents(mixed_raw, cut_points, tmpdir, short_id)

    # Mastering chain
    master_af = (
        "highpass=f=80,"
        "acompressor=threshold=-18dB:ratio=3:attack=10:release=100,"
        "equalizer=f=12000:width_type=o:width=2:g=2.5,"
        f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK_LIMIT}:LRA=11"
    )

    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", mixed_raw,
         "-af", master_af, "-t", str(target_duration),
         output_path],
        check=True, capture_output=True,
    )

    return output_path


def fetch_music_from_s3(
    mood: str,
    tmpdir: str,
    short_id: str,
) -> str | None:
    """Fetch background music from the pre-cached S3 music library.

    Checks s3://nexus-assets/music/manifest.json for available tracks,
    filters by mood, and downloads a random matching track.
    Returns local path or None if library is unavailable.
    """
    import boto3
    s3 = boto3.client("s3")
    try:
        manifest_obj = s3.get_object(Bucket=S3_ASSETS_BUCKET, Key="music/manifest.json")
        manifest = json.loads(manifest_obj["Body"].read())
    except Exception:
        return None

    tracks = manifest.get(mood, [])
    if not tracks:
        # Try partial mood match
        for key, values in manifest.items():
            if mood in key or key in mood:
                tracks = values
                break

    if not tracks:
        return None

    track_name = random.choice(tracks)
    s3_key = f"music/{mood}/{track_name}"
    local_path = os.path.join(tmpdir, f"music_{short_id}.mp3")
    try:
        s3.download_file(S3_ASSETS_BUCKET, s3_key, local_path)
        return local_path
    except Exception:
        return None


def fetch_music_clip(
    mood: str,
    duration: float,
    tmpdir: str,
    short_id: str,
    profile: dict | None = None,
) -> str | None:
    """Fetch background music, preferring the S3 library over Pixabay.

    For true_crime profiles, filters by profile["audio"]["music_mood"] == "dark_tension".
    Falls back to Pixabay API if S3 library is unavailable.
    """
    profile = profile or {}

    # Override mood for true_crime profiles
    if _is_true_crime(profile):
        mood = profile.get("audio", {}).get("music_mood", "dark_tension")

    # Tier 1: S3 pre-cached music library
    s3_track = fetch_music_from_s3(mood, tmpdir, short_id)
    if s3_track:
        return s3_track

    # Tier 2: Pixabay API (existing behaviour preserved)
    try:
        secret = get_secret("nexus/pexels_api_key")
        api_key = secret.get("pixabay_key", "")
        if not api_key:
            return None
    except Exception:
        return None

    import urllib.parse
    query = urllib.parse.quote(mood)
    url = f"https://pixabay.com/api/music/?key={api_key}&q={query}&per_page=3"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NexusCloud/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    hits = data.get("hits", [])
    if not hits:
        return None

    hit = hits[0]
    audio_field = hit.get("audio", {})
    music_url = (
        (audio_field.get("128") or audio_field.get("64"))
        if isinstance(audio_field, dict) else None
    ) or hit.get("previewURL", "")

    if not music_url or not str(music_url).startswith("http"):
        return None

    local_path = os.path.join(tmpdir, f"music_{short_id}.mp3")
    try:
        urllib.request.urlretrieve(music_url, local_path)
        return local_path
    except Exception:
        return None


def slice_from_main_audio(
    mixed_audio_key: str,
    start_time: float,
    duration: float,
    tmpdir: str,
    short_id: str,
) -> str | None:
    """Slice a segment from the main pipeline's mixed_audio.wav."""
    try:
        import boto3
        s3 = boto3.client("s3")
        local = os.path.join(tmpdir, f"main_audio_{short_id}.wav")
        s3.download_file(S3_ASSETS_BUCKET, mixed_audio_key, local)

        sliced = os.path.join(tmpdir, f"sliced_{short_id}.wav")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", local,
             "-ss", str(start_time), "-t", str(duration),
             sliced],
            check=True, capture_output=True,
        )
        return sliced
    except Exception:
        return None

