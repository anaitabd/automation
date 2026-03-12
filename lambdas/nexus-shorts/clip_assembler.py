"""FFmpeg filter_complex assembly for short-form clips."""

from __future__ import annotations

import os
import subprocess

from config import (
    FFMPEG_BIN, OUTPUT_CRF, OUTPUT_FPS, OUTPUT_HEIGHT, OUTPUT_WIDTH,
)


def assemble_clip(
    video_clips: list[str],
    cut_points: list[float],
    overlay_frames_dir: str | None,
    overlay_duration: float,
    overlay_start: float,
    transition_style: str,
    transition_duration: float,
    target_duration: float,
    output_path: str,
    tmpdir: str,
) -> str:
    """Assemble multiple video clips with transitions and overlays.

    Returns the path to the assembled video (no audio).
    """
    if not video_clips:
        raise ValueError("No video clips to assemble")

    if len(video_clips) == 1:
        # Single clip — just trim to duration
        _trim_clip(video_clips[0], target_duration, output_path)
    else:
        # Multi-clip assembly with transitions
        _multi_clip_assemble(
            video_clips, cut_points, transition_style,
            transition_duration, target_duration, output_path, tmpdir,
        )

    # Apply overlay if frame sequence exists
    if overlay_frames_dir and os.path.isdir(overlay_frames_dir):
        overlaid = output_path.replace(".mp4", "_ov.mp4")
        _apply_overlay_frames(
            output_path, overlay_frames_dir, overlay_start,
            overlay_duration, overlaid,
        )
        # Replace original with overlaid
        os.replace(overlaid, output_path)

    return output_path


def _trim_clip(input_path: str, duration: float, output_path: str) -> None:
    subprocess.run(
        [
            FFMPEG_BIN, "-y", "-i", input_path,
            "-t", str(duration),
            "-vf", f"fps={OUTPUT_FPS},scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
                   f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
            "-pix_fmt", "yuv420p", "-an", output_path,
        ],
        check=True, capture_output=True,
    )


def _multi_clip_assemble(
    clips: list[str],
    cut_points: list[float],
    transition: str,
    trans_dur: float,
    target_dur: float,
    output_path: str,
    tmpdir: str,
) -> None:
    """Assemble clips with xfade transitions."""
    # Calculate per-clip durations from cut points
    durations: list[float] = []
    prev = 0.0
    for cp in cut_points:
        durations.append(cp - prev)
        prev = cp
    durations.append(target_dur - prev)

    # Trim each clip to its target duration and normalize
    trimmed: list[str] = []
    for i, (clip, dur) in enumerate(zip(clips, durations)):
        t = os.path.join(tmpdir, f"trimmed_{i:03d}.mp4")
        subprocess.run(
            [
                FFMPEG_BIN, "-y", "-i", clip,
                "-t", str(max(0.5, dur)),
                "-vf", f"fps={OUTPUT_FPS},scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
                       f"force_original_aspect_ratio=decrease,"
                       f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "fast", "-crf", str(OUTPUT_CRF),
                "-pix_fmt", "yuv420p", "-an", t,
            ],
            check=True, capture_output=True,
        )
        trimmed.append(t)

    # Build xfade chain
    xfade_map = {
        "dissolve": "dissolve", "zoom_punch": "smoothup",
        "wipeleft": "wipeleft", "fade_black": "fadeblack", "cut": None,
    }
    xfade_name = xfade_map.get(transition, "dissolve")

    if xfade_name is None or len(trimmed) <= 1:
        # Simple concat
        list_file = os.path.join(tmpdir, "concat_assemble.txt")
        with open(list_file, "w") as f:
            for t in trimmed:
                f.write(f"file '{t}'\n")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", output_path],
            check=True, capture_output=True,
        )
    else:
        # Pairwise xfade
        current = trimmed[0]
        for i, next_clip in enumerate(trimmed[1:]):
            out = os.path.join(tmpdir, f"xfade_{i:03d}.mp4")
            offset = max(0.1, _get_duration(current) - trans_dur)
            subprocess.run(
                [
                    FFMPEG_BIN, "-y",
                    "-i", current, "-i", next_clip,
                    "-filter_complex",
                    f"[0][1]xfade=transition={xfade_name}:duration={trans_dur}:offset={offset}[v]",
                    "-map", "[v]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
                    "-pix_fmt", "yuv420p", out,
                ],
                check=True, capture_output=True,
            )
            current = out
        # Copy to final output
        os.replace(current, output_path)


def _apply_overlay_frames(
    video_path: str,
    frames_dir: str,
    start_time: float,
    duration: float,
    output_path: str,
) -> None:
    """Composite PNG frame sequence over video using FFmpeg."""
    # Find frame pattern
    frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    if not frames:
        import shutil
        shutil.copy2(video_path, output_path)
        return

    # Use image2 input with overlay
    pattern = os.path.join(frames_dir, frames[0].rsplit("_", 1)[0] + "_%04d.png")

    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-i", video_path,
            "-framerate", str(OUTPUT_FPS),
            "-start_number", "0",
            "-i", pattern,
            "-filter_complex",
            f"[1:v]format=rgba,setpts=PTS-STARTPTS+{start_time}/TB[ov];"
            f"[0:v][ov]overlay=0:0:enable='between(t,{start_time},{start_time + duration})'",
            "-c:v", "libx264", "-preset", "medium", "-crf", str(OUTPUT_CRF),
            "-pix_fmt", "yuv420p", "-c:a", "copy", output_path,
        ],
        check=True, capture_output=True,
    )


def _get_duration(path: str) -> float:
    import json
    from config import FFPROBE_BIN
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, check=True,
        )
        return float(json.loads(result.stdout).get("format", {}).get("duration", 5.0))
    except Exception:
        return 5.0

