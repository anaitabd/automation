"""LUT color grading + vignette + sharpening."""

from __future__ import annotations

import os
import subprocess

import boto3

from config import (
    FFMPEG_BIN, LUT_PRESETS, OUTPUT_CRF, S3_ASSETS_BUCKET,
)


def grade_clip(
    input_path: str,
    output_path: str,
    lut_preset: str,
    profile_name: str,
    tmpdir: str,
) -> str:
    """Apply LUT + vignette + sharpening to a clip.

    Falls back to FFmpeg curves filter if .cube file not available.
    """
    lut_s3_key = LUT_PRESETS.get(lut_preset, "")
    lut_local = None

    if lut_s3_key:
        lut_local = os.path.join(tmpdir, os.path.basename(lut_s3_key))
        if not os.path.exists(lut_local):
            try:
                s3 = boto3.client("s3")
                s3.download_file(S3_ASSETS_BUCKET, lut_s3_key, lut_local)
            except Exception:
                lut_local = None

    filters: list[str] = []

    if lut_local and os.path.exists(lut_local):
        filters.append(f"lut3d='{lut_local}':interp=trilinear")
    else:
        # Fallback curves approximation per profile
        curves = _fallback_curves(profile_name)
        if curves:
            filters.append(curves)

    # Sharpening
    filters.append("unsharp=5:5:0.8:3:3:0.4")

    # Vignette
    filters.append("vignette=PI/4")

    if not filters:
        # No grading needed — just copy
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path

    vf = ",".join(filters)

    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
            "-pix_fmt", "yuv420p", "-an", output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path


def _fallback_curves(profile_name: str) -> str:
    """Approximate color grade using FFmpeg curves filter."""
    if profile_name == "documentary":
        # Teal-orange approximation
        return "curves=r='0/0 0.25/0.28 0.5/0.55 0.75/0.77 1/1':g='0/0 0.5/0.45 1/0.95':b='0/0.05 0.25/0.28 0.5/0.5 1/0.85'"
    elif profile_name == "finance":
        # Cold blue
        return "curves=r='0/0 0.5/0.45 1/0.9':g='0/0 0.5/0.48 1/0.95':b='0/0.05 0.5/0.55 1/1'"
    elif profile_name == "entertainment":
        # High contrast punchy
        return "curves=r='0/0 0.2/0.1 0.5/0.55 0.8/0.9 1/1':g='0/0 0.5/0.45 1/0.95':b='0/0 0.5/0.45 1/0.9'"
    return ""

