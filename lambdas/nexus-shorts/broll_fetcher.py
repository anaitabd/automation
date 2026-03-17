"""B-roll acquisition with 4-tier fallback: Nova Reel → Pexels → Nova Canvas → gradient."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

import boto3

logger = logging.getLogger(__name__)

from config import (
    FFMPEG_BIN, NOVA_REEL_SHORTS_BUDGET, OUTPUT_HEIGHT, OUTPUT_WIDTH,
    S3_ASSETS_BUCKET, S3_OUTPUTS_BUCKET, SCRATCH_DIR,
)

try:
    import nova_canvas
    import nova_reel
except ImportError:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
    import nova_canvas
    import nova_reel

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


# ── Nova Reel parallel submission ────────────────────────────────────────────

def submit_nova_reel_jobs(
    clip_requests: list[dict],
    run_id: str,
    profile: dict,
) -> dict[str, str]:
    """Submit all Nova Reel jobs in parallel at batch start.

    Returns a dict mapping clip_id → invocation_arn.
    For true_crime profiles, Nova Reel budget is 0 (use Pexels + Nova Canvas instead).
    Only submits up to NOVA_REEL_SHORTS_BUDGET jobs.
    """
    profile_style = profile.get("script", {}).get("style", "")
    if profile_style == "true_crime":
        logger.info("true_crime profile: Nova Reel budget=0, skipping all Nova Reel jobs")
        return {}

    client = boto3.client("bedrock-runtime")
    invocations: dict[str, str] = {}

    for i, req in enumerate(clip_requests[:NOVA_REEL_SHORTS_BUDGET]):
        clip_id = req["clip_id"]
        prompt = req["prompt"]
        output_prefix = f"s3://{S3_OUTPUTS_BUCKET}/{run_id}/shorts/reel/{clip_id}"

        try:
            body = {
                "taskType": "TEXT_VIDEO",
                "textToVideoParams": {"text": prompt},
                "videoGenerationConfig": {
                    "durationSeconds": 6,
                    "fps": 24,
                    "dimension": "1280x720",
                    "seed": i,
                },
            }
            response = client.start_async_invoke(
                modelId="amazon.nova-reel-v1:0",
                modelInput=body,
                outputDataConfig={"s3OutputDataConfig": {"s3Uri": output_prefix}},
            )
            invocations[clip_id] = response["invocationArn"]
        except Exception as exc:
            logger.warning("Nova Reel submit failed for %s: %s", clip_id, exc)

    return invocations


def poll_nova_reel_job(
    invocation_arn: str,
    timeout: int = 120,
) -> str | None:
    """Poll a Nova Reel job until complete. Returns S3 URI or None on failure."""
    client = boto3.client("bedrock-runtime")
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = client.get_async_invoke(invocationArn=invocation_arn)
            status = resp.get("status", "")
            if status == "Completed":
                return resp.get("outputDataConfig", {}).get(
                    "s3OutputDataConfig", {}
                ).get("s3Uri", "")
            if status in ("Failed", "Cancelled"):
                return None
        except Exception:
            return None
        time.sleep(10)

    return None


# ── Pexels fallback ──────────────────────────────────────────────────────────

def fetch_pexels_clip(
    query: str,
    duration: float,
    tmpdir: str,
    clip_id: str,
    profile: dict | None = None,
) -> str | None:
    """Fetch a portrait-first clip from Pexels. Returns local path or None.

    For true_crime profiles, enriches the query with dark profile keywords
    and enforces portrait orientation first (9:16 aspect ratio for Shorts).
    """
    try:
        secret = get_secret("nexus/pexels_api_key")
        api_key = secret.get("api_key", "")
        if not api_key:
            return None
    except Exception:
        return None

    import urllib.parse

    # Enrich query with profile-specific Pexels keywords
    if profile:
        profile_keywords = profile.get("visuals", {}).get("pexels_keywords", [])
        if profile_keywords:
            extra = profile_keywords[0] if isinstance(profile_keywords, list) else str(profile_keywords)
            query = f"{query} {extra}"

    encoded_q = urllib.parse.quote(query)

    for orientation in ("portrait", "landscape"):
        url = (
            f"https://api.pexels.com/videos/search?query={encoded_q}"
            f"&orientation={orientation}&per_page=5&size=medium"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": api_key,
            "User-Agent": "NexusCloud/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue

        videos = data.get("videos", [])
        if not videos:
            continue

        # Pick best video file
        for video in videos:
            files = video.get("video_files", [])
            # Prefer HD portrait
            for vf in sorted(files, key=lambda f: f.get("height", 0), reverse=True):
                dl_url = vf.get("link", "")
                if not dl_url:
                    continue
                local_path = os.path.join(tmpdir, f"pexels_{clip_id}.mp4")
                try:
                    urllib.request.urlretrieve(dl_url, local_path)
                    return local_path
                except Exception:
                    continue

    return None


# ── Nova Canvas + motion fallback ────────────────────────────────────────────

def generate_canvas_with_motion(
    prompt: str,
    clip_id: str,
    duration: float,
    tmpdir: str,
) -> str | None:
    """Generate a still image with Nova Canvas and animate it with FFmpeg."""
    try:
        image_bytes = nova_canvas.generate_image(
            prompt=prompt,
            width=OUTPUT_WIDTH,
            height=OUTPUT_HEIGHT,
            quality="standard",
        )
    except Exception:
        return None

    img_path = os.path.join(tmpdir, f"canvas_{clip_id}.png")
    with open(img_path, "wb") as f:
        f.write(image_bytes)

    out_path = os.path.join(tmpdir, f"canvas_motion_{clip_id}.mp4")

    import subprocess
    # Ken Burns zoom-in animation on still
    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-loop", "1", "-i", img_path,
            "-vf", (
                f"scale={int(OUTPUT_WIDTH * 1.2)}:{int(OUTPUT_HEIGHT * 1.2)},"
                f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
                f"'(iw-{OUTPUT_WIDTH})/2*(1-t/{duration})':"
                f"'(ih-{OUTPUT_HEIGHT})/2*(1-t/{duration})'"
            ),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-t", str(duration),
            out_path,
        ],
        check=True, capture_output=True,
    )
    return out_path


# ── Brand gradient fallback (always works) ───────────────────────────────────

def generate_brand_gradient(
    primary_color: str,
    secondary_color: str,
    clip_id: str,
    duration: float,
    tmpdir: str,
) -> str:
    """Generate a brand gradient + particle field. Never fails."""
    from PIL import Image, ImageDraw
    import subprocess

    primary = _hex_to_rgb(primary_color)
    secondary = _hex_to_rgb(secondary_color)

    img = Image.new("RGB", (OUTPUT_WIDTH, OUTPUT_HEIGHT))
    draw = ImageDraw.Draw(img)

    for row in range(OUTPUT_HEIGHT):
        frac = row / OUTPUT_HEIGHT
        r = int(primary[0] * (1 - frac) + secondary[0] * frac)
        g = int(primary[1] * (1 - frac) + secondary[1] * frac)
        b = int(primary[2] * (1 - frac) + secondary[2] * frac)
        draw.line([(0, row), (OUTPUT_WIDTH, row)], fill=(r, g, b))

    img_path = os.path.join(tmpdir, f"gradient_{clip_id}.png")
    img.save(img_path)

    out_path = os.path.join(tmpdir, f"gradient_{clip_id}.mp4")
    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-loop", "1", "-i", img_path,
            "-vf", (
                f"scale={int(OUTPUT_WIDTH * 1.1)}:{int(OUTPUT_HEIGHT * 1.1)},"
                f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:"
                f"'(iw-{OUTPUT_WIDTH})/2+sin(t*0.5)*20':"
                f"'(ih-{OUTPUT_HEIGHT})/2+cos(t*0.3)*30'"
            ),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-t", str(duration),
            out_path,
        ],
        check=True, capture_output=True,
    )
    return out_path


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.strip().lstrip("#")
    if len(color) < 6:
        color = color.ljust(6, "0")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


# ── Main fetch function with 4-tier fallback ─────────────────────────────────

def fetch_broll_clip(
    clip_id: str,
    prompt: str,
    search_query: str,
    duration: float,
    primary_color: str,
    secondary_color: str,
    nova_invocations: dict[str, str],
    tmpdir: str,
    profile: dict | None = None,
) -> str:
    """Fetch a single b-roll clip with 4-tier fallback. Always returns a path."""

    # Tier 1: Nova Reel
    if clip_id in nova_invocations:
        arn = nova_invocations[clip_id]
        s3_uri = poll_nova_reel_job(arn, timeout=120)
        if s3_uri:
            # Download the mp4 from S3
            local = os.path.join(tmpdir, f"reel_{clip_id}.mp4")
            try:
                s3 = boto3.client("s3")
                bucket = s3_uri.replace("s3://", "").split("/")[0]
                prefix = "/".join(s3_uri.replace("s3://", "").split("/")[1:])
                # Find the .mp4 file
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/")):
                    for obj in page.get("Contents", []):
                        if obj["Key"].endswith(".mp4"):
                            s3.download_file(bucket, obj["Key"], local)
                            return local
            except Exception:
                pass

    # Tier 2: Pexels (portrait-first for Shorts 9:16)
    pexels_clip = fetch_pexels_clip(search_query, duration, tmpdir, clip_id, profile=profile)
    if pexels_clip:
        return pexels_clip

    # Tier 3: Nova Canvas + motion
    canvas_clip = generate_canvas_with_motion(prompt, clip_id, duration, tmpdir)
    if canvas_clip:
        return canvas_clip

    # Tier 4: Brand gradient (never fails)
    return generate_brand_gradient(
        primary_color, secondary_color, clip_id, duration, tmpdir
    )

