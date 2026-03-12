"""Voiceover + music mixing and mastering to -14 LUFS."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
import time

from config import (
    FFMPEG_BIN, FFPROBE_BIN, OUTPUT_AUDIO_BITRATE,
    S3_ASSETS_BUCKET, TARGET_LUFS, TRUE_PEAK_LIMIT,
)

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        import boto3
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def mix_audio(
    voiceover_path: str,
    music_path: str | None,
    target_duration: float,
    short_id: str,
    tmpdir: str,
) -> str:
    """Mix voiceover + background music and master to -14 LUFS.

    Mastering chain:
    1. High-pass filter at 80Hz (remove rumble)
    2. Compression: -18dB threshold, 3:1 ratio, 10ms attack
    3. Air boost EQ: +2.5dB shelf at 12kHz
    4. Loudness normalization: -14 LUFS
    5. True peak limiter: -1dBTP
    """
    output_path = os.path.join(tmpdir, f"mixed_{short_id}.wav")

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
            f"[1:a]afade=t=in:st=0:d=1,volume=0.12,afade=t=out:st={target_duration - 1}:d=1[music];"
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
                f"[1:a]volume=0.10[music];"
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


def fetch_music_clip(
    mood: str,
    duration: float,
    tmpdir: str,
    short_id: str,
) -> str | None:
    """Fetch background music from Pixabay Music API."""
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

