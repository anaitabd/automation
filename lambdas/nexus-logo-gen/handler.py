"""nexus-logo-gen — Nova Canvas channel logo generation.

Generates a 1024×1024 channel logo using Amazon Nova Canvas,
styled to match the brand kit colors. Falls back to a simple
Pillow-rendered text logo if Nova Canvas fails.
"""

import json
import logging
import os

import boto3

from nexus_pipeline_utils import get_logger

# shared/ is copied into the Lambda directory by deploy_tf.sh
try:
    from shared.nova_canvas import generate_and_upload_image
except ImportError:
    from nova_canvas import generate_and_upload_image

log = get_logger("nexus-logo-gen")

ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")


def _generate_fallback_logo(channel_name: str, brand: dict, s3_key: str) -> str:
    """Generate a simple text-based logo using Pillow when Nova Canvas fails."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    size = 1024
    img = Image.new("RGBA", (size, size), brand.get("secondary_color", "#1A1A2E"))
    draw = ImageDraw.Draw(img)

    # Draw a gradient circle background
    primary = brand.get("primary_color", "#4F6EF7")
    accent = brand.get("accent_color", "#FFD700")

    # Circle
    margin = 80
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=primary,
        outline=accent,
        width=8,
    )

    # Initials text
    initials = "".join(w[0].upper() for w in channel_name.split() if w)[:2]
    if not initials:
        initials = channel_name[:2].upper()

    # Use default font at large size
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 280)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - 20
    draw.text((x, y), initials, fill="#FFFFFF", font=font)

    # Upload
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=ASSETS_BUCKET,
        Key=s3_key,
        Body=buf.getvalue(),
        ContentType="image/png",
    )
    return s3_key


def lambda_handler(event: dict, context) -> dict:
    channel_id = event["channel_id"]
    channel_name = event["channel_name"]
    niche = event.get("niche", "")
    profile = event.get("profile", "documentary")
    brand = event.get("brand", {})

    primary = brand.get("primary_color", "#4F6EF7")
    secondary = brand.get("secondary_color", "#1A1A2E")
    accent = brand.get("accent_color", "#FFD700")
    font_name = brand.get("font", "Cinzel")

    s3_key = f"channels/{channel_id}/logo.png"

    prompt = (
        f"Professional YouTube channel logo for '{channel_name}', "
        f"topic: {niche}, style: {profile}. "
        f"Color palette: {primary}, {secondary}, {accent}. "
        f"Clean modern design, centered icon/monogram, "
        f"dark {secondary} background, {primary} and {accent} accents. "
        f"Minimalist, scalable, no text, no watermark, high quality, "
        f"suitable for profile picture and video watermark. "
        f"Square composition 1:1."
    )

    negative_prompt = (
        "text, words, letters, watermark, blurry, low quality, "
        "distorted, ugly, amateur, cluttered, busy background, "
        "multiple objects, hands, faces"
    )

    log.info("Generating logo for channel '%s' via Nova Canvas", channel_name)

    try:
        result_key = generate_and_upload_image(
            prompt=prompt,
            s3_key=s3_key,
            bucket=ASSETS_BUCKET,
            negative_prompt=negative_prompt,
            width=1024,
            height=1024,
            quality="premium",
            cfg_scale=9.0,
            seed=0,
        )
        log.info("Logo generated via Nova Canvas: %s", result_key)
        return {"channel_id": channel_id, "logo_s3_key": result_key}

    except Exception as exc:
        log.warning("Nova Canvas logo generation failed: %s — using Pillow fallback", exc)
        try:
            fallback_key = _generate_fallback_logo(channel_name, brand, s3_key)
            log.info("Fallback logo generated: %s", fallback_key)
            return {"channel_id": channel_id, "logo_s3_key": fallback_key}
        except Exception as fallback_exc:
            log.error("Fallback logo generation also failed: %s", fallback_exc)
            raise RuntimeError(f"Logo generation failed: Nova Canvas={exc}, Pillow={fallback_exc}")

