"""Channel logo watermark overlay."""

from __future__ import annotations

import os
import subprocess

import boto3

from config import FFMPEG_BIN, OUTPUT_CRF, S3_ASSETS_BUCKET


def apply_watermark(
    video_path: str,
    output_path: str,
    logo_s3_key: str | None,
    tmpdir: str,
    opacity: float = 0.75,
) -> str:
    """Composite channel logo at top center with specified opacity.

    If no logo available, copies video as-is.
    """
    if not logo_s3_key:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    logo_local = os.path.join(tmpdir, "channel_logo.png")
    if not os.path.exists(logo_local):
        try:
            s3 = boto3.client("s3")
            s3.download_file(S3_ASSETS_BUCKET, logo_s3_key, logo_local)
        except Exception:
            import shutil
            shutil.copy2(video_path, output_path)
            return output_path

    # Composite logo at top center with opacity
    vf = (
        f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.2f},"
        f"scale=120:-2[logo];"
        f"[0:v][logo]overlay=(W-w)/2:30"
    )

    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-i", video_path, "-i", logo_local,
            "-filter_complex", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
            "-pix_fmt", "yuv420p", "-c:a", "copy", output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path

