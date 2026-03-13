"""nexus-intro-outro — Generate channel intro/outro video clips.

Uses Nova Reel for 6s animated intro and outro clips, styled to match
the channel brand kit. Falls back to Pillow + FFmpeg static-to-video
rendering if Nova Reel fails.
"""

import io
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
except ImportError:
    from nova_reel import generate_and_upload_video

log = get_logger("nexus-intro-outro")

ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
INTRO_DURATION = int(os.environ.get("INTRO_DURATION_SEC", "6"))
OUTRO_DURATION = int(os.environ.get("OUTRO_DURATION_SEC", "6"))


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Convert hex color string to RGB tuple."""
    color = color.strip().lstrip("#")
    if len(color) < 6:
        color = color.ljust(6, "0")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _generate_frame(
    channel_name: str,
    brand: dict,
    width: int,
    height: int,
    text_line: str,
    subtext: str = "",
) -> bytes:
    """Generate a single branded frame as PNG bytes using Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    bg_color = brand.get("secondary_color", "#1A1A2E")
    primary = brand.get("primary_color", "#4F6EF7")
    accent = brand.get("accent_color", "#FFD700")

    img = Image.new("RGBA", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Draw accent line at top
    draw.rectangle([0, 0, width, 6], fill=accent)

    # Draw channel name / text
    try:
        font_large = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72
        )
    except (IOError, OSError):
        font_large = ImageFont.load_default()

    try:
        font_small = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36
        )
    except (IOError, OSError):
        font_small = ImageFont.load_default()

    # Main text centered
    bbox = draw.textbbox((0, 0), text_line, font=font_large)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = (height - th) // 2 - 40
    draw.text((x, y), text_line, fill=primary, font=font_large)

    # Subtext below
    if subtext:
        bbox2 = draw.textbbox((0, 0), subtext, font=font_small)
        tw2 = bbox2[2] - bbox2[0]
        x2 = (width - tw2) // 2
        y2 = y + th + 30
        draw.text((x2, y2), subtext, fill="#CCCCCC", font=font_small)

    # Draw accent line at bottom
    draw.rectangle([0, height - 6, width, height], fill=accent)

    # Draw decorative circle behind text
    cx, cy = width // 2, height // 2
    radius = 180
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        outline=primary,
        width=3,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _frames_to_video(
    frame_png: bytes,
    duration_sec: int,
    output_path: str,
    fps: int = 30,
) -> str:
    """Convert a static PNG frame to a video file using FFmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(frame_png)
        tmp_path = tmp.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", tmp_path,
            "-c:v", "libx264",
            "-t", str(duration_sec),
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-crf", "18",
            "-preset", "fast",
            "-movflags", "+faststart",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return output_path
    finally:
        os.unlink(tmp_path)


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


def _generate_clip_fallback(
    channel_name: str,
    brand: dict,
    s3_key: str,
    text_line: str,
    subtext: str,
    duration_sec: int,
) -> str:
    """Generate a clip via Pillow + FFmpeg fallback."""
    frame_png = _generate_frame(
        channel_name=channel_name,
        brand=brand,
        width=1280,
        height=720,
        text_line=text_line,
        subtext=subtext,
    )

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        output_path = tmp.name

    try:
        _frames_to_video(frame_png, duration_sec, output_path)

        s3 = boto3.client("s3")
        final_key = s3_key if s3_key.endswith(".mp4") else s3_key + ".mp4"
        s3.upload_file(output_path, ASSETS_BUCKET, final_key)
        log.info("Fallback clip uploaded: %s", final_key)
        return final_key
    finally:
        if os.path.exists(output_path):
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
        f"Cinematic channel intro animation for '{channel_name}', "
        f"topic: {niche}, style: {profile}. "
        f"Dark {secondary} background with {primary} light rays converging to center, "
        f"{accent} particle effects, smooth camera push-in, "
        f"professional broadcast quality, no text, no faces."
    )
    try:
        intro_key = _generate_clip_nova_reel(intro_prompt, intro_s3_key, INTRO_DURATION)
        log.info("Intro generated via Nova Reel: %s", intro_key)
    except Exception as exc:
        log.warning("Nova Reel intro failed: %s — using Pillow+FFmpeg fallback", exc)
        try:
            intro_key = _generate_clip_fallback(
                channel_name, brand, intro_s3_key,
                text_line=channel_name,
                subtext=tagline or f"Exploring {niche}",
                duration_sec=INTRO_DURATION,
            )
        except Exception as fallback_exc:
            log.error("Intro generation failed entirely: %s", fallback_exc)
            raise RuntimeError(
                f"Intro generation failed: Nova Reel={exc}, Fallback={fallback_exc}"
            )

    # ── Outro clip ──────────────────────────────────────────────
    outro_prompt = (
        f"Cinematic channel outro animation for '{channel_name}', "
        f"topic: {niche}, style: {profile}. "
        f"Dark {secondary} background, {primary} and {accent} elements fading out, "
        f"smooth camera pull-back, elegant ending motion, "
        f"professional broadcast quality, no text, no faces."
    )
    try:
        outro_key = _generate_clip_nova_reel(outro_prompt, outro_s3_key, OUTRO_DURATION)
        log.info("Outro generated via Nova Reel: %s", outro_key)
    except Exception as exc:
        log.warning("Nova Reel outro failed: %s — using Pillow+FFmpeg fallback", exc)
        try:
            outro_key = _generate_clip_fallback(
                channel_name, brand, outro_s3_key,
                text_line="Thanks for watching!",
                subtext=channel_name,
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

