"""Beat detection and cut-point snapping using librosa."""

from __future__ import annotations

from config import BPM_DEFAULTS


def detect_beats(audio_path: str, profile_name: str = "documentary") -> list[float]:
    """Detect beat timestamps in an audio file.

    Uses profile-specific BPM start estimate for better detection.
    Returns a list of beat timestamps in seconds.
    """
    try:
        import librosa
        import numpy as np

        start_bpm = BPM_DEFAULTS.get(profile_name, 90)
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(
            y=y, sr=sr, start_bpm=start_bpm
        )
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        if not beat_times:
            raise ValueError("Empty beat list")
        return beat_times
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Beat detection failed (%s) — using fixed intervals", exc)
        return []


def snap_cut_points(
    raw_cuts: list[float],
    beats: list[float],
    tolerance: float = 0.4,
    min_gap: float = 3.0,
) -> list[float]:
    """Snap raw cut points to nearest beat within tolerance.

    Enforces minimum gap of min_gap seconds between consecutive cuts.
    """
    snapped: list[float] = []

    for cut in raw_cuts:
        # Find nearest beat
        if beats:
            closest = min(beats, key=lambda b: abs(b - cut))
            if abs(closest - cut) <= tolerance:
                cut = closest

        # Enforce minimum gap
        if snapped and (cut - snapped[-1]) < min_gap:
            continue

        snapped.append(cut)

    return snapped


def generate_cut_points(
    total_duration: float,
    num_clips: int,
    beats: list[float],
    profile_name: str = "documentary",
) -> list[float]:
    """Generate evenly-spaced cut points and snap to beats.

    Returns a list of timestamps where cuts should occur.
    """
    if num_clips <= 1:
        return []

    interval = total_duration / num_clips
    raw_cuts = [interval * i for i in range(1, num_clips)]

    return snap_cut_points(raw_cuts, beats)


def find_loop_point(
    target_duration: float,
    beats: list[float],
    tolerance: float = 0.5,
) -> float:
    """Find the best beat-aligned loop point near target_duration.

    Returns the timestamp closest to target_duration from the beat list.
    """
    if not beats:
        return target_duration

    # Find beat closest to target
    candidates = [b for b in beats if abs(b - target_duration) <= tolerance]
    if candidates:
        return min(candidates, key=lambda b: abs(b - target_duration))

    # No beat in range — use target directly
    return target_duration

