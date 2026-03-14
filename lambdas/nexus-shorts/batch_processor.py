"""Batch processing with ThreadPoolExecutor + per-short retry logic."""

from __future__ import annotations

import os
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    SHORTS_MAX_WORKERS, SCRATCH_DIR, TIER_DEFS,
    OUTPUT_FPS, OUTPUT_HEIGHT, OUTPUT_WIDTH,
)
from section_scorer import select_sections
from script_condenser import condense_sections
from voiceover_generator import generate_voiceover
from broll_fetcher import fetch_broll_clip
from vertical_converter import convert_to_vertical
from color_grader import grade_clip
from motion_renderer import render_overlay
from beat_syncer import detect_beats, generate_cut_points, find_loop_point
from clip_assembler import assemble_clip
from loop_builder import build_loop
from audio_mixer import mix_audio, fetch_music_clip
from watermarker import apply_watermark
from uploader import upload_short, write_error

import logging

log = logging.getLogger("nexus-shorts")

# Per-short status tracking
STATUS_PENDING = "pending"
STATUS_ACQUIRING = "acquiring"
STATUS_CONDENSING = "condensing"
STATUS_VOICING = "voicing"
STATUS_RENDERING = "rendering"
STATUS_BLENDING = "blending"
STATUS_UPLOADING = "uploading"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def process_batch(
    tiers_requested: list[str],
    script: dict,
    profile: dict,
    profile_name: str,
    run_id: str,
    brand_kit: dict,
    nova_invocations: dict[str, str],
    mixed_audio_s3_key: str,
    dry_run: bool = False,
) -> list[dict]:
    """Process all requested tiers in parallel.

    Returns a list of per-short result dicts for the manifest.
    Individual failures never stop the batch.
    """
    tasks: list[dict] = []
    sections = script.get("sections", script.get("scenes", []))

    for tier in tiers_requested:
        tier_def = TIER_DEFS.get(tier)
        if not tier_def:
            log.warning("Unknown tier '%s' — skipping", tier)
            continue

        short_id = f"short_{tier}_001"
        tasks.append({
            "short_id": short_id,
            "tier": tier,
            "tier_def": tier_def,
            "sections": sections,
        })

    if dry_run:
        return [
            {
                "short_id": t["short_id"],
                "tier": t["tier"],
                "duration": t["tier_def"]["duration"],
                "status": "success",
                "s3_key": f"{run_id}/shorts/short_{t['tier']}_001_dry_run.mp4",
                "dry_run": True,
            }
            for t in tasks
        ]

    results: list[dict] = []

    def _process_one(task: dict) -> dict:
        return _process_single_short(
            task=task,
            script=script,
            profile=profile,
            profile_name=profile_name,
            run_id=run_id,
            brand_kit=brand_kit,
            nova_invocations=nova_invocations,
            mixed_audio_s3_key=mixed_audio_s3_key,
        )

    with ThreadPoolExecutor(max_workers=SHORTS_MAX_WORKERS) as executor:
        futures = {executor.submit(_process_one, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                log.error("Short %s failed: %s", task["short_id"], exc)
                write_error(run_id, task["short_id"], str(exc))
                results.append({
                    "short_id": task["short_id"],
                    "tier": task["tier"],
                    "duration": task["tier_def"]["duration"],
                    "status": STATUS_FAILED,
                    "error": str(exc),
                    "attempts": 1,
                })

    return results


def _process_single_short(
    task: dict,
    script: dict,
    profile: dict,
    profile_name: str,
    run_id: str,
    brand_kit: dict,
    nova_invocations: dict[str, str],
    mixed_audio_s3_key: str,
    max_attempts: int = 2,
) -> dict:
    """Process a single short with retry logic."""
    short_id = task["short_id"]
    tier = task["tier"]
    tier_def = task["tier_def"]
    target_duration = tier_def["duration"]
    all_sections = task["sections"]

    accent_color = brand_kit.get("accent_color", "#C8A96E")
    primary_color = brand_kit.get("primary_color", accent_color)
    secondary_color = brand_kit.get("secondary_color", "#1a1a2e")
    logo_s3_key = brand_kit.get("logo_s3_key")
    shorts_cfg = profile.get("shorts", {})
    overlay_style = shorts_cfg.get("overlay_style", "kinetic_title")
    transition_style = shorts_cfg.get("transition_style", "dissolve")
    lut_preset = shorts_cfg.get("lut_preset", "teal_orange")
    music_mood = profile.get("sound_design", {}).get("music_mood", "tension_atmospheric")

    last_error = None

    for attempt in range(1, max_attempts + 1):
        tmpdir = tempfile.mkdtemp(
            dir=SCRATCH_DIR if os.path.isdir(SCRATCH_DIR) else None,
            prefix=f"short_{short_id}_",
        )
        try:
            # 1. Select best sections for this tier
            log.info("[%s] Selecting %d–%d sections", short_id,
                     tier_def["sections_min"], tier_def["sections_max"])
            selected = select_sections(
                all_sections, profile_name, tier_def["sections_max"]
            )

            # 2. Condense script
            log.info("[%s] Condensing %d sections → short narration", short_id, len(selected))
            narration = condense_sections(
                selected, tier, target_duration, profile
            )

            # 3. Generate voiceover
            log.info("[%s] Generating voiceover", short_id)
            vo_path, _ = generate_voiceover(
                narration, short_id, profile, target_duration, tmpdir, run_id
            )

            # 4. Detect beats
            log.info("[%s] Detecting beats", short_id)
            beats = detect_beats(vo_path, profile_name)

            # 5. Fetch b-roll clips
            num_clips = tier_def["nova_clips"]
            log.info("[%s] Fetching %d b-roll clips", short_id, num_clips)
            broll_clips: list[str] = []

            for ci in range(num_clips):
                clip_id = f"{short_id}_clip{ci:02d}"
                # Build prompt from section content
                sec_idx = ci % len(selected)
                sec = selected[sec_idx]
                visual_prompt = sec.get("nova_canvas_prompt",
                                       sec.get("visual_cue", {}).get("search_queries", ["cinematic"])[0]
                                       if isinstance(sec.get("visual_cue", {}).get("search_queries"), list)
                                       else "cinematic landscape")
                search_query = sec.get("title", "cinematic footage")

                raw_clip = fetch_broll_clip(
                    clip_id=clip_id,
                    prompt=visual_prompt,
                    search_query=search_query,
                    duration=target_duration / num_clips + 1.5,
                    primary_color=primary_color,
                    secondary_color=secondary_color,
                    nova_invocations=nova_invocations,
                    tmpdir=tmpdir,
                )

                # Convert to vertical
                vert_path = os.path.join(tmpdir, f"vert_{clip_id}.mp4")
                convert_to_vertical(raw_clip, vert_path)
                broll_clips.append(vert_path)

            # 6. Color grade all clips
            log.info("[%s] Color grading %d clips", short_id, len(broll_clips))
            graded_clips: list[str] = []
            for i, clip in enumerate(broll_clips):
                graded = os.path.join(tmpdir, f"graded_{i:03d}.mp4")
                grade_clip(clip, graded, lut_preset, profile_name, tmpdir)
                graded_clips.append(graded)

            # 7. Render motion overlay
            log.info("[%s] Rendering %s overlay", short_id, overlay_style)
            overlay_dir = os.path.join(tmpdir, "overlay_frames")
            overlay_text = narration.split(".")[0][:60] if narration else ""
            overlay_frames = render_overlay(
                overlay_type=overlay_style,
                text=overlay_text,
                accent_color=accent_color,
                duration=min(3.0, target_duration * 0.15),
                out_dir=overlay_dir,
                subtitle=script.get("title", "")[:40],
                total_duration=target_duration,
            )
            overlay_duration = min(3.0, target_duration * 0.15) if overlay_frames else 0

            # 8. Generate cut points snapped to beats
            cut_points = generate_cut_points(
                target_duration + 1.5, len(graded_clips), beats, profile_name
            )

            # 9. Assemble clips
            log.info("[%s] Assembling video", short_id)
            assembled = os.path.join(tmpdir, f"assembled_{short_id}.mp4")
            assemble_clip(
                video_clips=graded_clips,
                cut_points=cut_points,
                overlay_frames_dir=overlay_dir if overlay_frames else None,
                overlay_duration=overlay_duration,
                overlay_start=0.5,
                transition_style=transition_style,
                transition_duration=0.3,
                target_duration=target_duration + 1.5,
                output_path=assembled,
                tmpdir=tmpdir,
            )

            # 10. Build seamless loop
            log.info("[%s] Building seamless loop", short_id)
            loop_point = find_loop_point(target_duration, beats)
            looped = os.path.join(tmpdir, f"looped_{short_id}.mp4")
            looped, actual_loop = build_loop(
                assembled, target_duration, loop_point, looped, tmpdir
            )

            # 11. Mix audio
            log.info("[%s] Mixing audio", short_id)
            bg_music = fetch_music_clip(music_mood, target_duration, tmpdir, short_id)
            mixed = mix_audio(vo_path, bg_music, target_duration, short_id, tmpdir)

            # 12. Mux video + audio
            log.info("[%s] Final mux", short_id)
            muxed = os.path.join(tmpdir, f"muxed_{short_id}.mp4")
            import subprocess
            from config import FFMPEG_BIN, OUTPUT_CRF
            subprocess.run(
                [
                    FFMPEG_BIN, "-y",
                    "-i", looped, "-i", mixed,
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", "slow", "-crf", str(OUTPUT_CRF),
                    "-profile:v", "high", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    "-t", str(target_duration),
                    muxed,
                ],
                check=True, capture_output=True,
            )

            # 13. Apply watermark
            log.info("[%s] Applying watermark", short_id)
            final = os.path.join(tmpdir, f"final_{short_id}.mp4")
            apply_watermark(muxed, final, logo_s3_key, tmpdir)

            # 14. Upload
            log.info("[%s] Uploading to S3", short_id)
            s3_key = upload_short(final, run_id, short_id, tier)

            log.info("[%s] ✅ Complete — %s", short_id, s3_key)
            return {
                "short_id": short_id,
                "tier": tier,
                "duration": target_duration,
                "s3_key": s3_key,
                "loop_point": actual_loop,
                "beat_synced": len(beats) > 0,
                "overlay_type": overlay_style,
                "sections_used": len(selected),
                "attempts": attempt,
                "status": "success",
            }

        except Exception as exc:
            last_error = exc
            log.warning("[%s] Attempt %d/%d failed: %s",
                        short_id, attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(10 * attempt)
        finally:
            # Cleanup tmpdir
            try:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    # All attempts exhausted
    error_msg = str(last_error) if last_error else "Unknown error"
    write_error(run_id, short_id, error_msg)
    return {
        "short_id": short_id,
        "tier": tier,
        "duration": target_duration,
        "status": STATUS_FAILED,
        "error": error_msg,
        "attempts": max_attempts,
    }

