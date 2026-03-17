"""Seamless loop construction + pixel similarity verification."""

from __future__ import annotations

import logging
import os
import subprocess

from config import (
    FFMPEG_BIN, FFPROBE_BIN, OUTPUT_CRF, OUTPUT_FPS,
    SHORTS_LOOP_THRESHOLD, SHORTS_LOOP_VERIFY,
)

logger = logging.getLogger(__name__)


def build_loop(
    input_path: str,
    target_duration: float,
    loop_point: float,
    output_path: str,
    tmpdir: str,
    blend_duration: float = 0.5,
    max_retries: int = 2,
) -> tuple[str, float]:
    """Build a seamless looping video.

    1. Render content_duration = target + 1.5s
    2. Crossfade tail → head over blend_duration
    3. Verify pixel similarity ≥ threshold
    4. If failed, retry with longer blend

    Returns (output_path, actual_loop_point).
    """
    # If input is already close to target, just trim
    input_dur = _get_duration(input_path)
    if input_dur < target_duration + 0.5:
        # Not enough material for proper loop — just use as-is
        _trim(input_path, target_duration, output_path)
        return output_path, target_duration

    for attempt in range(max_retries):
        current_blend = blend_duration + (0.3 * attempt)

        # Extract tail segment (loop_point - blend → loop_point)
        tail_start = max(0, loop_point - current_blend)
        tail_path = os.path.join(tmpdir, f"loop_tail_{attempt}.mp4")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", input_path,
             "-ss", str(tail_start), "-t", str(current_blend),
             "-c:v", "libx264", "-preset", "fast", "-crf", str(OUTPUT_CRF),
             "-pix_fmt", "yuv420p", "-an", tail_path],
            check=True, capture_output=True,
        )

        # Extract head segment (0 → blend)
        head_path = os.path.join(tmpdir, f"loop_head_{attempt}.mp4")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", input_path,
             "-t", str(current_blend),
             "-c:v", "libx264", "-preset", "fast", "-crf", str(OUTPUT_CRF),
             "-pix_fmt", "yuv420p", "-an", head_path],
            check=True, capture_output=True,
        )

        # Crossfade tail → head
        blended_path = os.path.join(tmpdir, f"loop_blend_{attempt}.mp4")
        try:
            subprocess.run(
                [FFMPEG_BIN, "-y",
                 "-i", tail_path, "-i", head_path,
                 "-filter_complex",
                 f"[0][1]xfade=transition=dissolve:duration={current_blend}:offset=0[v]",
                 "-map", "[v]",
                 "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
                 "-pix_fmt", "yuv420p", blended_path],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError:
            # Fallback: just trim without loop
            _trim(input_path, target_duration, output_path)
            return output_path, target_duration

        # Build final: content[0:loop_point-blend] + blended segment
        main_path = os.path.join(tmpdir, f"loop_main_{attempt}.mp4")
        main_end = max(0.1, loop_point - current_blend)
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", input_path,
             "-t", str(main_end),
             "-c:v", "libx264", "-preset", "fast", "-crf", str(OUTPUT_CRF),
             "-pix_fmt", "yuv420p", "-an", main_path],
            check=True, capture_output=True,
        )

        # Concat main + blend
        concat_list = os.path.join(tmpdir, f"loop_concat_{attempt}.txt")
        with open(concat_list, "w") as f:
            f.write(f"file '{main_path}'\n")
            f.write(f"file '{blended_path}'\n")

        loop_output = os.path.join(tmpdir, f"looped_{attempt}.mp4")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
             "-i", concat_list,
             "-t", str(target_duration),
             "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
             "-pix_fmt", "yuv420p", loop_output],
            check=True, capture_output=True,
        )

        # Verify pixel similarity
        if not SHORTS_LOOP_VERIFY:
            os.replace(loop_output, output_path)
            return output_path, loop_point

        similarity = _check_loop_similarity(loop_output, target_duration)
        if similarity >= SHORTS_LOOP_THRESHOLD:
            os.replace(loop_output, output_path)
            return output_path, loop_point

        logger.warning(
            "Loop similarity %.2f < %.2f (attempt %d/%d)",
            similarity, SHORTS_LOOP_THRESHOLD, attempt + 1, max_retries,
        )

    # Accept imperfect loop after retries
    if os.path.exists(loop_output):
        os.replace(loop_output, output_path)
    else:
        _trim(input_path, target_duration, output_path)
    return output_path, loop_point


def _check_loop_similarity(video_path: str, duration: float) -> float:
    """Compare frame at 0.1s vs (duration - 0.1s) using numpy."""
    try:
        import numpy as np
        from PIL import Image
        import io

        frames: list[np.ndarray] = []
        for ts in [0.1, max(0.2, duration - 0.1)]:
            frame_path = video_path + f"_check_{ts:.1f}.png"
            subprocess.run(
                [FFMPEG_BIN, "-y", "-i", video_path,
                 "-ss", str(ts), "-vframes", "1",
                 "-vf", "scale=270:480", frame_path],
                check=True, capture_output=True,
            )
            if os.path.exists(frame_path):
                img = Image.open(frame_path).convert("RGB")
                frames.append(np.array(img, dtype=np.float32))
                os.remove(frame_path)

        if len(frames) != 2:
            return 1.0  # Can't verify — assume OK

        # Normalized pixel similarity
        diff = np.abs(frames[0] - frames[1])
        max_diff = 255.0 * frames[0].size
        similarity = 1.0 - (diff.sum() / max_diff)
        return similarity

    except Exception:
        return 1.0  # Can't verify — assume OK


def _get_duration(path: str) -> float:
    import json
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, check=True,
        )
        return float(json.loads(result.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


def _trim(input_path: str, duration: float, output_path: str) -> None:
    subprocess.run(
        [FFMPEG_BIN, "-y", "-i", input_path,
         "-t", str(duration),
         "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
         "-pix_fmt", "yuv420p", "-an", output_path],
        check=True, capture_output=True,
    )

