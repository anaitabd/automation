"""nexus-intro-outro — Generate channel intro/outro video clips.

Primary:  Nova Reel 6-second cinematic animation.
Fallback: Nova Canvas still image + FFmpeg Ken Burns zoom + fade.
Last resort: pure FFmpeg animated gradient (no text, always cinematic).
"""

import json
import logging
import os
import subprocess
import tempfile

import boto3

from nexus_pipeline_utils import get_logger

# shared/ is copied into the Lambda directory by deploy_tf.sh
try:
    from shared.nova_reel import generate_and_upload_video
    from shared import nova_canvas
except ImportError:
    from nova_reel import generate_and_upload_video
    import nova_canvas

log = get_logger("nexus-intro-outro")

ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
INTRO_DURATION = int(os.environ.get("INTRO_DURATION_SEC", "6"))
OUTRO_DURATION = int(os.environ.get("OUTRO_DURATION_SEC", "6"))


def _apply_ken_burns(
    png_path: str,
    output_path: str,
    duration_sec: int,
    clip_type: str = "intro",
    fps: int = 30,
) -> None:
    """Apply slow Ken Burns zoom + fade-in/fade-out to a still PNG image via FFmpeg."""
    total_frames = duration_sec * fps
    fade_out_start = max(0, duration_sec - 1)
    # Intro: slow cinematic push-in; Outro: slow elegant pull-back
    zoom_expr = "min(zoom+0.0012,1.07)" if clip_type == "intro" else "max(zoom-0.0012,1.0)"
    vf = (
        f"zoompan=z='{zoom_expr}':d={total_frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720,"
        f"fade=t=in:st=0:d=1,"
        f"fade=t=out:st={fade_out_start}:d=1"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", png_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-t", str(duration_sec),
        "-r", str(fps),
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "fast",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def _generate_gradient_video(
    output_path: str,
    duration_sec: int,
    secondary_hex: str = "#1A1A2E",
    fps: int = 30,
) -> None:
    """Generate a cinematic animated dark gradient via FFmpeg (no text, no images)."""
    bg = secondary_hex.lstrip("#")
    fade_out_start = max(0, duration_sec - 1)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x{bg}:s=1280x720:r={fps}",
        "-vf",
        f"fade=t=in:st=0:d=1,fade=t=out:st={fade_out_start}:d=1,vignette=PI/4",
        "-t", str(duration_sec),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-preset", "fast",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


def _generate_clip_nova_reel(
    prompt: str,
    s3_key: str,
    duration_sec: int,
) -> str:
    """Generate a clip via Nova Reel and return the S3 key."""
    result_key = generate_and_upload_video(
        text_prompt=prompt,
        output_s3_key=s3_key,
        output_s3_bucket=ASSETS_BUCKET,
        duration_seconds=duration_sec,
        width=1280,
        height=720,
        seed=0,
    )
    log.info("Nova Reel clip generated: %s", result_key)
    return result_key


def _generate_clip_canvas_fallback(
    brand: dict,
    niche: str,
    profile: str,
    s3_key: str,
    clip_type: str,
    duration_sec: int,
) -> str:
    """Generate a cinematic clip via Nova Canvas still + Ken Burns FFmpeg effect.

    Falls back to a pure FFmpeg animated gradient if Nova Canvas is unavailable.
    Never renders text of any kind.
    """
    secondary = brand.get("secondary_color", "#1A1A2E")

    if clip_type == "intro":
        canvas_prompt = (
            f"Cinematic wide establishing shot for a {profile} YouTube channel "
            f"about {niche}. Dramatic volumetric god rays, deep atmospheric shadows, "
            f"bokeh depth of field, rich dark blue tones, lens flare, "
            f"professional broadcast quality, no text, no people, no faces, "
            f"no watermarks, no logos, 4K ultra-cinematic."
        )
    else:
        canvas_prompt = (
            f"Cinematic fade-out wide shot for a {profile} YouTube channel "
            f"about {niche}. Golden atmospheric glow dissolving into peaceful darkness, "
            f"serene wide-angle composition, rich cinematic color grade, "
            f"professional broadcast quality, no text, no people, no faces, "
            f"no watermarks, no logos, 4K ultra-cinematic."
        )

    png_path = None
    output_path = None
    try:
        image_bytes = nova_canvas.generate_image(
            prompt=canvas_prompt,
            negative_prompt=(
                "text, watermark, caption, title, logo, letters, words, numbers, "
                "typography, subtitles, blurry, low quality, cartoon, ugly, face, person"
            ),
            width=1280,
            height=720,
            quality="premium",
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as png_tmp:
            png_tmp.write(image_bytes)
            png_path = png_tmp.name
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as vid_tmp:
            output_path = vid_tmp.name
        _apply_ken_burns(png_path, output_path, duration_sec, clip_type)
        log.info("Nova Canvas + Ken Burns fallback succeeded for %s", clip_type)
    except Exception as canvas_exc:
        log.warning("Nova Canvas fallback failed: %s — using gradient animation", canvas_exc)
        if output_path is None:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as vid_tmp:
                output_path = vid_tmp.name
        _generate_gradient_video(output_path, duration_sec, secondary)
    finally:
        if png_path and os.path.exists(png_path):
            os.unlink(png_path)

    try:
        s3 = boto3.client("s3")
        final_key = s3_key if s3_key.endswith(".mp4") else s3_key + ".mp4"
        s3.upload_file(output_path, ASSETS_BUCKET, final_key)
        log.info("Cinematic fallback clip uploaded: %s", final_key)
        return final_key
    finally:
        if output_path and os.path.exists(output_path):
            os.unlink(output_path)


def lambda_handler(event: dict, context) -> dict:
    channel_id = event["channel_id"]
    channel_name = event["channel_name"]
    niche = event.get("niche", "")
    profile = event.get("profile", "documentary")
    brand = event.get("brand", {})

    primary = brand.get("primary_color", "#4F6EF7")
    secondary = brand.get("secondary_color", "#1A1A2E")
    accent = brand.get("accent_color", "#FFD700")
    tagline = brand.get("tagline", "")

    intro_s3_key = f"channels/{channel_id}/intro.mp4"
    outro_s3_key = f"channels/{channel_id}/outro.mp4"

    log.info(
        "Generating intro/outro for channel '%s' (id=%s, profile=%s)",
        channel_name, channel_id, profile,
    )

    # ── Intro clip ──────────────────────────────────────────────
    intro_prompt = (
        f"Cinematic opening sequence for a {profile} documentary-style YouTube channel "
        f"about {niche}. Epic slow camera push-in through volumetric god rays and smoke, "
        f"deep atmospheric shadows, rich dark {secondary} tones with luminous {primary} "
        f"highlights, {accent} particle embers floating upward, bokeh depth of field, "
        f"lens flare, ultra-high production quality. "
        f"Absolutely no text, no words, no captions, no watermarks, no faces, no people. "
        f"Hollywood cinematic broadcast quality."
    )
    try:
        intro_key = _generate_clip_nova_reel(intro_prompt, intro_s3_key, INTRO_DURATION)
        log.info("Intro generated via Nova Reel: %s", intro_key)
    except Exception as exc:
        log.warning("Nova Reel intro failed: %s — using cinematic canvas fallback", exc)
        try:
            intro_key = _generate_clip_canvas_fallback(
                brand=brand,
                niche=niche,
                profile=profile,
                s3_key=intro_s3_key,
                clip_type="intro",
                duration_sec=INTRO_DURATION,
            )
        except Exception as fallback_exc:
            log.error("Intro generation failed entirely: %s", fallback_exc)
            raise RuntimeError(
                f"Intro generation failed: Nova Reel={exc}, Fallback={fallback_exc}"
            )

    # ── Outro clip ──────────────────────────────────────────────
    outro_prompt = (
        f"Cinematic closing sequence for a {profile} documentary-style YouTube channel "
        f"about {niche}. Slow elegant camera pull-back revealing a vast atmospheric "
        f"landscape, {accent} golden light dissolving gently into deep dark {secondary} "
        f"shadows, {primary} rim light accents, peaceful epic fade-out, soft bokeh, "
        f"ultra-high production quality. "
        f"Absolutely no text, no words, no captions, no watermarks, no faces, no people. "
        f"Hollywood cinematic broadcast quality."
    )
    try:
        outro_key = _generate_clip_nova_reel(outro_prompt, outro_s3_key, OUTRO_DURATION)
        log.info("Outro generated via Nova Reel: %s", outro_key)
    except Exception as exc:
        log.warning("Nova Reel outro failed: %s — using cinematic canvas fallback", exc)
        try:
            outro_key = _generate_clip_canvas_fallback(
                brand=brand,
                niche=niche,
                profile=profile,
                s3_key=outro_s3_key,
                clip_type="outro",
                duration_sec=OUTRO_DURATION,
            )
        except Exception as fallback_exc:
            log.error("Outro generation failed entirely: %s", fallback_exc)
            raise RuntimeError(
                f"Outro generation failed: Nova Reel={exc}, Fallback={fallback_exc}"
            )

    return {
        "channel_id": channel_id,
        "intro_s3_key": intro_key,
        "outro_s3_key": outro_key,
    }

