import base64
import json
import os
import time

import boto3
import psycopg2

from nexus_pipeline_utils import get_logger

log = get_logger("nexus-channel-setup")

_cache: dict = {}

S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")

BEDROCK_MODEL_ID = "anthropic.claude-3-sonnet-20240229-v1:0"
NOVA_CANVAS_MODEL_ID = "amazon.nova-canvas-v1:0"

GPU_INSTANCE_ID = os.environ.get("GPU_INSTANCE_ID", "")


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def _get_db_conn():
    creds = get_secret("nexus/db_credentials")
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["dbname"],
        user=creds["user"],
        password=creds["password"],
        connect_timeout=10,
    )


def _generate_visual_identity(channel_name: str, niche: str, profile: str) -> dict:
    client = boto3.client("bedrock-runtime")
    prompt = (
        f"You are a brand designer. Create a visual identity for a YouTube channel.\n"
        f"Channel name: {channel_name}\n"
        f"Niche: {niche}\n"
        f"Content profile: {profile}\n\n"
        "Return ONLY a JSON object with these exact keys:\n"
        "- color_palette: object with primary, secondary, accent, background (hex codes)\n"
        "- font_style: one of (bold_sans, elegant_serif, modern_minimal, cinematic, playful)\n"
        "- logo_prompt: detailed image generation prompt for a logo (max 200 chars)\n"
        "- banner_prompt: detailed image generation prompt for a channel banner (max 200 chars)\n"
        "- intro_prompt: text-to-video prompt for a 5-second animated logo intro (max 150 chars)\n"
        "- outro_prompt: text-to-video prompt for an 8-second subscribe CTA outro (max 150 chars)\n"
        "No markdown, no explanation."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    })
    for attempt in range(3):
        try:
            response = client.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            raw = json.loads(response["body"].read())["content"][0]["text"]
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(raw)
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"Visual identity generation failed: {exc}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _nova_canvas_text_image(image_prompt: str, width: int, height: int) -> bytes:
    client = boto3.client("bedrock-runtime")
    body = json.dumps({
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {
            "text": image_prompt,
            "negativeText": "blurry, low quality, watermark, text overlay",
        },
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "width": width,
            "height": height,
            "quality": "premium",
            "cfgScale": 7.5,
        },
    })
    for attempt in range(3):
        try:
            response = client.invoke_model(
                modelId=NOVA_CANVAS_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(response["body"].read())
            b64 = result["images"][0]
            return base64.b64decode(b64)
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f"Nova Canvas image generation failed: {exc}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _upload_bytes_to_s3(data: bytes, s3_key: str, content_type: str) -> str:
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=S3_ASSETS_BUCKET,
        Key=s3_key,
        Body=data,
        ContentType=content_type,
    )
    return f"s3://{S3_ASSETS_BUCKET}/{s3_key}"


def _generate_wan_video(
    channel_id: str,
    prompt: str,
    duration_sec: float,
    asset_name: str,
    image_s3_key: str | None = None,
) -> str:
    from gpu_client import generate_clip, start_ec2, stop_ec2

    gpu_server_url = get_secret("nexus/gpu_server_url")["url"]

    if GPU_INSTANCE_ID:
        start_ec2(GPU_INSTANCE_ID)

    try:
        s3_key = generate_clip(
            server_url=gpu_server_url,
            prompt=prompt,
            image_s3_key=image_s3_key,
            duration_sec=duration_sec,
            run_id=f"channel-{channel_id}",
            section_idx=0,
        )
    finally:
        if GPU_INSTANCE_ID:
            stop_ec2(GPU_INSTANCE_ID)

    return f"s3://{S3_ASSETS_BUCKET}/{s3_key}"


def _update_channel_branding(
    channel_id: str,
    logo_s3_url: str,
    banner_s3_url: str,
    intro_video_s3_url: str,
    outro_video_s3_url: str,
    color_palette: dict,
    font_style: str,
) -> None:
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE channels
                SET logo_s3_url = %s,
                    banner_s3_url = %s,
                    intro_video_s3_url = %s,
                    outro_video_s3_url = %s,
                    color_palette = %s,
                    font_style = %s,
                    status = 'active'
                WHERE id = %s
                """,
                (
                    logo_s3_url,
                    banner_s3_url,
                    intro_video_s3_url,
                    outro_video_s3_url,
                    json.dumps(color_palette),
                    font_style,
                    channel_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def lambda_handler(event: dict, context) -> dict:
    channel_id = event["channel_id"]
    channel_name = event["channel_name"]
    niche = event["niche"]
    profile = event.get("profile", "documentary")

    log.info("Channel setup start: channel_id=%s name=%s niche=%s", channel_id, channel_name, niche)

    identity = _generate_visual_identity(channel_name, niche, profile)
    color_palette = identity["color_palette"]
    font_style = identity["font_style"]
    logo_prompt = identity["logo_prompt"]
    banner_prompt = identity["banner_prompt"]
    intro_prompt = identity["intro_prompt"]
    outro_prompt = identity["outro_prompt"]

    log.info("Visual identity generated: font=%s colors=%s", font_style, color_palette)

    logo_bytes = _nova_canvas_text_image(logo_prompt, width=1024, height=1024)
    logo_s3_key = f"channels/{channel_id}/branding/logo.png"
    logo_s3_url = _upload_bytes_to_s3(logo_bytes, logo_s3_key, "image/png")
    log.info("Logo uploaded: %s", logo_s3_url)

    banner_bytes = _nova_canvas_text_image(banner_prompt, width=2560, height=1440)
    banner_s3_key = f"channels/{channel_id}/branding/banner.png"
    banner_s3_url = _upload_bytes_to_s3(banner_bytes, banner_s3_key, "image/png")
    log.info("Banner uploaded: %s", banner_s3_url)

    intro_video_s3_url = _generate_wan_video(
        channel_id=channel_id,
        prompt=intro_prompt,
        duration_sec=5.0,
        asset_name="intro.mp4",
        image_s3_key=logo_s3_key,
    )
    log.info("Intro video generated: %s", intro_video_s3_url)

    outro_video_s3_url = _generate_wan_video(
        channel_id=channel_id,
        prompt=outro_prompt,
        duration_sec=8.0,
        asset_name="outro.mp4",
        image_s3_key=logo_s3_key,
    )
    log.info("Outro video generated: %s", outro_video_s3_url)

    _update_channel_branding(
        channel_id=channel_id,
        logo_s3_url=logo_s3_url,
        banner_s3_url=banner_s3_url,
        intro_video_s3_url=intro_video_s3_url,
        outro_video_s3_url=outro_video_s3_url,
        color_palette=color_palette,
        font_style=font_style,
    )

    log.info("Channel %s branding complete", channel_id)
    return {
        "channel_id": channel_id,
        "logo_s3_url": logo_s3_url,
        "banner_s3_url": banner_s3_url,
        "intro_video_s3_url": intro_video_s3_url,
        "outro_video_s3_url": outro_video_s3_url,
        "color_palette": color_palette,
        "font_style": font_style,
        "status": "complete",
    }
