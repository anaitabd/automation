"""Landscape → 1080×1920 vertical conversion with 3 strategies."""

from __future__ import annotations

import os
import subprocess

from config import FFMPEG_BIN, FFPROBE_BIN, OUTPUT_HEIGHT, OUTPUT_WIDTH


def _get_dimensions(path: str) -> tuple[int, int]:
    """Get video width and height."""
    import json
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return int(stream.get("width", 0)), int(stream.get("height", 0))
    except Exception:
        pass
    return 0, 0


def convert_to_vertical(
    input_path: str,
    output_path: str,
    strategy: str = "auto",
) -> str:
    """Convert any aspect ratio to 1080×1920 vertical.

    Strategies:
    - 'auto': detect aspect ratio and pick best strategy
    - 'smart_crop': center crop over blurred background
    - 'split_screen': top half sharp, bottom half mirrored+blurred
    - 'full_blur': fit inside 1080w, centered on full blurred bg
    """
    w, h = _get_dimensions(input_path)

    if strategy == "auto":
        if w == 0 or h == 0:
            strategy = "smart_crop"
        elif h >= w:
            # Already portrait — just scale
            strategy = "passthrough"
        elif w / max(h, 1) > 2.0:
            # Very wide — use split screen
            strategy = "split_screen"
        else:
            strategy = "smart_crop"

    if strategy == "passthrough":
        return _passthrough(input_path, output_path)
    elif strategy == "split_screen":
        return _split_screen(input_path, output_path)
    elif strategy == "full_blur":
        return _full_blur(input_path, output_path)
    else:
        return _smart_crop(input_path, output_path)


def _passthrough(input_path: str, output_path: str) -> str:
    """Portrait source — scale to 1080×1920 directly."""
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", input_path,
            "-vf", (
                f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
            ),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path


def _smart_crop(input_path: str, output_path: str) -> str:
    """Strategy A: Smart crop + blurred background (default)."""
    vf = (
        # Blurred background
        f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        f"gblur=sigma=40,colorlevels=rimin=0.3:gimin=0.3:bimin=0.3[bg];"
        # Sharp foreground — scale to fill width, crop center
        f"[0:v]scale={OUTPUT_WIDTH}:-2,"
        f"crop={OUTPUT_WIDTH}:min(ih\\,{OUTPUT_HEIGHT})[fg];"
        # Composite
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"vignette=PI/4"
    )
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", input_path,
            "-filter_complex", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path


def _split_screen(input_path: str, output_path: str) -> str:
    """Strategy B: Top half sharp, bottom half mirrored + blurred."""
    half_h = OUTPUT_HEIGHT // 2
    vf = (
        # Top half — main content
        f"[0:v]scale={OUTPUT_WIDTH}:-2,crop={OUTPUT_WIDTH}:{half_h}[top];"
        # Bottom half — mirrored + blurred
        f"[0:v]scale={OUTPUT_WIDTH}:-2,crop={OUTPUT_WIDTH}:{half_h},"
        f"vflip,gblur=sigma=30[bot];"
        # Stack with 2px accent line
        f"[top][bot]vstack,"
        f"drawbox=x=0:y={half_h - 1}:w={OUTPUT_WIDTH}:h=2:color=0xC8A96E@0.8:t=fill"
    )
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", input_path,
            "-filter_complex", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path


def _full_blur(input_path: str, output_path: str) -> str:
    """Strategy C: Full blurred background with centered foreground."""
    vf = (
        # Background — stretch + heavy blur
        f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        f"gblur=sigma=50[bg];"
        # Foreground — fit within width
        f"[0:v]scale={OUTPUT_WIDTH}:-2[fg];"
        # Composite
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", input_path,
            "-filter_complex", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an", output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path

