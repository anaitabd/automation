import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import boto3
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-thumbnail")

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")

# NVIDIA NIM — takes priority over Bedrock when set
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# Vision model for frame scoring; text model for concept generation
NVIDIA_VISION_MODEL = os.environ.get("NVIDIA_VISION_MODEL", "microsoft/phi-3.5-vision-instruct")
NVIDIA_TEXT_MODEL = os.environ.get("NVIDIA_TEXT_MODEL", "meta/llama-3.1-70b-instruct")

STABILITY_API_KEY = os.environ.get("STABILITY_API_KEY", "")
STABILITY_API_URL = "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image"

NVIDIA_FLUX_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"


def _find_bin(name: str) -> str:
    """Locate a binary (ffmpeg / ffprobe) across Lambda-layer and system paths."""
    for candidate in (f"/opt/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    import shutil
    path = shutil.which(name)
    if path:
        return path
    raise FileNotFoundError(f"{name} not found. Install it or set the {name.upper()}_BIN env var.")


FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or _find_bin("ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or _find_bin("ffprobe")
BEDROCK_MODEL_ID_DEFAULT = "anthropic.claude-3-5-sonnet-20241022-v2:0"

# Set dynamically per-invocation from the loaded profile
_active_model_id: str = BEDROCK_MODEL_ID_DEFAULT


def _http_post(url: str, headers: dict, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            merged = {"User-Agent": "NexusCloud/1.0"}
            merged.update(headers)
            req = urllib.request.Request(url, data=data, headers=merged, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _get_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 60.0


def _extract_keyframes(video_path: str, tmpdir: str, n: int = 6) -> list[str]:
    duration = _get_duration(video_path)
    start = duration * 0.10
    end = duration * 0.90
    usable = end - start
    if usable <= 0:
        start, usable = 0, duration

    frame_paths = []
    for i in range(n):
        ts = start + (usable / (n - 1)) * i if n > 1 else start + usable / 2
        out_path = os.path.join(tmpdir, f"frame_{i:02d}.jpg")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-ss", str(ts), "-i", video_path,
             "-vframes", "1", "-q:v", "2", out_path],
            capture_output=True, check=False,
        )
        if os.path.exists(out_path):
            frame_paths.append(out_path)

    return frame_paths


# ---------------------------------------------------------------------------
# NVIDIA NIM helpers
# ---------------------------------------------------------------------------

def _nvidia_chat(messages: list, model: str, max_tokens: int = 1024) -> str:
    """Call NVIDIA NIM OpenAI-compatible chat endpoint. Returns response text."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        NVIDIA_BASE_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def _score_frame_nvidia(frame_path: str) -> float:
    """Score a video frame for thumbnail quality using NVIDIA vision model."""
    with open(frame_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Rate this YouTube thumbnail frame on a scale of 0.0 to 1.0 based on: "
                    "contrast, subject clarity, emotional impact, and legibility at small size. "
                    "Respond with ONLY a JSON object: {\"score\": 0.0}"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            },
        ],
    }]
    try:
        raw = _nvidia_chat(messages, NVIDIA_VISION_MODEL, max_tokens=64)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return float(json.loads(raw).get("score", 0.5))
    except Exception as exc:
        log.warning("NVIDIA vision score failed: %s", exc)
        return 0.5


def _generate_thumbnail_concepts_nvidia(title: str, mood: str, accent_color: str) -> list[dict]:
    """Generate 3 thumbnail concepts using NVIDIA NIM text model."""
    prompt = (
        f"You are a YouTube thumbnail strategist. Create 3 distinct thumbnail concepts for:\n"
        f"Title: {title}\nMood: {mood}\nAccent color: {accent_color}\n\n"
        "For each concept provide:\n"
        "- top_text: max 4 words, ALL CAPS, high-impact\n"
        "- sub_text: max 6 words, title case\n"
        "- emotion_trigger: one word (fear/curiosity/excitement/awe/shock)\n"
        "- color_scheme: one of (dark_dramatic/bright_energetic/cinematic_cold/warm_epic)\n\n"
        "Return ONLY a JSON array of 3 objects with these exact keys. No markdown."
    )
    for attempt in range(3):
        try:
            raw = _nvidia_chat([{"role": "user", "content": prompt}], NVIDIA_TEXT_MODEL, max_tokens=1024)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            concepts = json.loads(raw)
            return concepts[:3]
        except Exception as exc:
            log.warning("NVIDIA concept gen attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return []


# ---------------------------------------------------------------------------
# Bedrock helpers (fallback when NVIDIA_API_KEY is not set)
# ---------------------------------------------------------------------------

def _score_frame_bedrock(frame_path: str) -> float:
    """Score a frame using AWS Bedrock Claude vision. Falls back to 0.5 on error."""
    if NVIDIA_API_KEY:
        return _score_frame_nvidia(frame_path)

    with open(frame_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    client = boto3.client("bedrock-runtime")
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Rate this YouTube thumbnail frame on a scale of 0.0 to 1.0 based on: "
                                "contrast, subject clarity, emotional impact, and legibility at small size. "
                                "Respond with ONLY a JSON object: {\"score\": 0.0}"
                            ),
                        },
                    ],
                }
            ],
        }
    )
    try:
        response = client.invoke_model(
            modelId=_active_model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        raw = json.loads(response["body"].read())["content"][0]["text"]
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        return float(data.get("score", 0.5))
    except Exception:
        return 0.5


def _generate_thumbnail_concepts(
    title: str, mood: str, accent_color: str
) -> list[dict]:
    """Generate thumbnail concepts. Uses NVIDIA NIM if NVIDIA_API_KEY is set, else Bedrock."""
    if NVIDIA_API_KEY:
        concepts = _generate_thumbnail_concepts_nvidia(title, mood, accent_color)
        if concepts:
            log.info("Thumbnail concepts generated via NVIDIA NIM (%s)", NVIDIA_TEXT_MODEL)
            return concepts
        log.warning("NVIDIA concept generation returned empty — falling back to Bedrock")

    client = boto3.client("bedrock-runtime")
    prompt = (
        f"You are a YouTube thumbnail strategist. Create 3 distinct thumbnail concepts for:\n"
        f"Title: {title}\nMood: {mood}\nAccent color: {accent_color}\n\n"
        "For each concept provide:\n"
        "- top_text: max 4 words, ALL CAPS, high-impact\n"
        "- sub_text: max 6 words, title case\n"
        "- emotion_trigger: one word (fear/curiosity/excitement/awe/shock)\n"
        "- color_scheme: one of (dark_dramatic/bright_energetic/cinematic_cold/warm_epic)\n\n"
        "Return ONLY a JSON array of 3 objects with these exact keys. No markdown."
    )
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    retries = 3
    for attempt in range(retries):
        try:
            response = client.invoke_model(
                modelId=_active_model_id,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            raw = json.loads(response["body"].read())["content"][0]["text"]
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            concepts = json.loads(raw)
            return concepts[:3]
        except Exception:
            if attempt == retries - 1:
                return [
                    {"top_text": "UNTOLD STORY", "sub_text": title[:30], "emotion_trigger": "curiosity", "color_scheme": "dark_dramatic"},
                    {"top_text": "SHOCKING TRUTH", "sub_text": title[:30], "emotion_trigger": "shock", "color_scheme": "cinematic_cold"},
                    {"top_text": "YOU WON'T BELIEVE", "sub_text": title[:30], "emotion_trigger": "awe", "color_scheme": "warm_epic"},
                ]
            time.sleep(2 ** attempt)
    return []


def _hex_to_0x(color: str) -> str:
    """Convert '#RRGGBB' to '0xRRGGBB' so ffmpeg doesn't treat # as comment."""
    if color.startswith("#"):
        return "0x" + color[1:]
    return color


def _hex_to_rgba(color: str, alpha: int = 255) -> tuple:
    """Convert '#RRGGBB' or '0xRRGGBB' to (R, G, B, A) tuple."""
    color = color.strip()
    if color.startswith("#"):
        color = color[1:]
    elif color.lower().startswith("0x"):
        color = color[2:]
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha)


def _find_font(name: str) -> str:
    """Search common font directories for a TTF font file."""
    candidates = [
        f"/usr/share/fonts/dejavu-sans-fonts/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/{name}",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


THUMBNAIL_FONT = _find_font("DejaVuSans-Bold.ttf")
THUMBNAIL_FONT_LIGHT = _find_font("DejaVuSans.ttf")


def _ensure_pillow() -> bool:
    """Ensure Pillow is importable, installing it to /tmp/pillow_deps if necessary."""
    import sys
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        pass
    # Add /tmp/pillow_deps to path and retry (warm Lambda reuse of a prior install)
    deps_dir = "/tmp/pillow_deps"
    if deps_dir not in sys.path:
        sys.path.insert(0, deps_dir)
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        pass
    # Attempt runtime install into /tmp (fallback when layer is missing Pillow)
    try:
        log.info("Pillow not found — installing to /tmp/pillow_deps...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "Pillow",
             "--target", deps_dir, "-q", "--no-cache-dir"],
            check=True, capture_output=True,
        )
        # Verify the install actually made Pillow importable
        from PIL import Image  # noqa: F401
        return True
    except Exception as exc:
        log.warning("Could not install or import Pillow: %s", exc)
        return False


def _pil_load_font(font_path: str, size: int):
    """Load PIL ImageFont, falling back to default."""
    try:
        from PIL import ImageFont
        if font_path and os.path.isfile(font_path):
            return ImageFont.truetype(font_path, size)
    except Exception:
        pass
    try:
        from PIL import ImageFont
        return ImageFont.load_default()
    except Exception:
        return None


def _pil_draw_text_centered(draw, text: str, font, y: int, img_w: int,
                             fill: tuple, shadow: tuple = None, shadow_offset: int = 3):
    """Draw centered text with optional shadow."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = len(text) * (getattr(font, "size", 20))
    x = (img_w - tw) // 2
    if shadow:
        draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _generate_nvidia_flux_background(concept_text: str, out_path: str) -> bool:
    api_key = NVIDIA_API_KEY
    if not api_key:
        try:
            api_key = get_secret("nexus/nvidia_api_key").get("api_key", "")
        except Exception:
            pass
    if not api_key:
        return False
    try:
        prompt = (
            f"YouTube thumbnail background, cinematic, {concept_text}, "
            "4K, photorealistic, no text, no watermark, no logo"
        )
        body = json.dumps({
            "prompt": prompt,
            "mode": "base",
            "cfg_scale": 3.5,
            "width": 1280,
            "height": 720,
            "seed": 0,
            "steps": 50,
        }).encode("utf-8")
        req = urllib.request.Request(
            NVIDIA_FLUX_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                log.warning("NVIDIA FLUX API returned status %d", resp.status)
                return False
            result = json.loads(resp.read())
        artifacts = result.get("artifacts", [])
        if artifacts:
            img_b64 = artifacts[0].get("base64", "")
        else:
            data_list = result.get("data", [])
            img_b64 = data_list[0].get("b64_json", "") if data_list else ""
        if not img_b64:
            log.warning("NVIDIA FLUX returned no image data")
            return False
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(img_b64))
        log.info("NVIDIA FLUX background generated: %s", out_path)
        return True
    except Exception as exc:
        log.warning("NVIDIA FLUX background generation failed: %s — using fallback", exc)
        return False


def _generate_stability_background(concept_text: str, out_path: str) -> bool:
    # Generate a 1280x720 background image using Stability AI from the thumbnail concept string.
    # Returns True if successful, False if Stability AI is not configured or the call fails.
    # Falls back gracefully — the caller uses the video frame instead.
    api_key = STABILITY_API_KEY
    if not api_key:
        try:
            api_key = get_secret("nexus/stability_api_key").get("api_key", "")
        except Exception:
            pass
    if not api_key:
        # TODO: set STABILITY_API_KEY env var or store under "nexus/stability_api_key" to enable AI backgrounds
        return False
    try:
        import urllib.request as _req
        body = json.dumps({
            "text_prompts": [
                {"text": f"YouTube thumbnail background, cinematic, {concept_text}, 4K, no text, no watermark", "weight": 1.0},
                {"text": "text, watermark, logo, blurry, low quality", "weight": -1.0},
            ],
            "cfg_scale": 7,
            "width": 1280,
            "height": 720,
            "steps": 30,
            "samples": 1,
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = _req.Request(STABILITY_API_URL, data=body, headers=headers, method="POST")
        with _req.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        artifacts = result.get("artifacts", [])
        if not artifacts:
            log.warning("Stability AI returned no artifacts")
            return False
        img_b64 = artifacts[0].get("base64", "")
        if not img_b64:
            return False
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(img_b64))
        log.info("Stability AI background generated: %s", out_path)
        return True
    except Exception as exc:
        log.warning("Stability AI background generation failed: %s — using video frame", exc)
        return False


def _render_thumbnail(
    frame_path: str,
    concept: dict,
    profile: dict,
    tmpdir: str,
    idx: int,
) -> str:
    out_path = os.path.join(tmpdir, f"thumbnail_{idx}.jpg")
    accent_raw = profile.get("thumbnail", {}).get("accent_color", "#C8A96E")
    channel_name = profile.get("name", "Nexus").upper()
    top_text = concept.get("top_text", "")[:45]
    sub_text = concept.get("sub_text", "")[:45]

    concept_desc = f"{concept.get('emotion_trigger', '')} {concept.get('color_scheme', '')} {top_text} {sub_text}".strip()
    ai_bg = os.path.join(tmpdir, f"thumb_bg_{idx}.jpg")

    used_ai_bg = _generate_nvidia_flux_background(concept_desc, ai_bg)
    if not used_ai_bg:
        used_ai_bg = _generate_stability_background(concept_desc, ai_bg)

    eq_path = os.path.join(tmpdir, f"eq_{idx}.jpg")
    if used_ai_bg:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", ai_bg,
             "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
             "-q:v", "2", eq_path],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", frame_path,
             "-vf", "eq=contrast=1.15:saturation=1.25:brightness=0.02",
             "-vframes", "1", "-q:v", "2", eq_path],
            check=True, capture_output=True,
        )

    # Step 2: PIL text overlays (stroke/borderw=4 equivalent via shadow_offset)
    if not _ensure_pillow():
        # Pillow unavailable — return base image without text
        import shutil
        shutil.copy(eq_path, out_path)
        return out_path

    from PIL import Image, ImageDraw

    ar, ag, ab, _ = _hex_to_rgba(accent_raw)

    img = Image.open(eq_path).convert("RGBA")
    W, H = img.size

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Dark gradient box over bottom 45%
    box_top = int(H * 0.55)
    for row in range(H - box_top):
        alpha = int(191 * (row / (H - box_top)))
        draw.line([(0, box_top + row), (W, box_top + row)], fill=(0, 0, 0, alpha))

    # top_text — large, centered at y=40, white with 4px stroke effect (shadow_offset=4)
    font_top = _pil_load_font(THUMBNAIL_FONT, 88)
    _pil_draw_text_centered(
        draw, top_text, font_top, y=40, img_w=W,
        fill=(255, 255, 255, 255),
        shadow=(0, 0, 0, 230),
        shadow_offset=4,
    )

    # sub_text — medium, centered at y=60% height, light gray with 4px stroke effect
    font_sub = _pil_load_font(THUMBNAIL_FONT_LIGHT, 52)
    _pil_draw_text_centered(
        draw, sub_text, font_sub, y=int(H * 0.60), img_w=W,
        fill=(221, 221, 221, 255),
        shadow=(0, 0, 0, 200),
        shadow_offset=4,
    )

    # Channel badge — accent-colored box top-right with channel name
    badge_x = W - 280
    draw.rectangle([(badge_x, 10), (W - 10, 70)], fill=(ar, ag, ab, 229))
    font_badge = _pil_load_font(THUMBNAIL_FONT, 24)
    try:
        bbox_b = draw.textbbox((0, 0), channel_name, font=font_badge)
        bw = bbox_b[2] - bbox_b[0]
    except Exception:
        bw = len(channel_name) * 14
    bx = badge_x + ((270 - bw) // 2)
    draw.text((bx, 28), channel_name, font=font_badge, fill=(255, 255, 255, 255))

    img = Image.alpha_composite(img, overlay).convert("RGB")
    img.save(out_path, "JPEG", quality=92)
    return out_path


def _write_error(run_id: str, step: str, exc: Exception) -> None:
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/errors/{step}.json",
            Body=json.dumps({"step": step, "error": str(exc)}).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    final_video_s3_key: str = event["final_video_s3_key"]
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    title_passthrough: str = event.get("title", "")
    video_duration_sec: float = float(event.get("video_duration_sec", 0))

    step_start = notify_step_start("thumbnail", run_id, niche=event.get("niche", ""), profile=profile_name, dry_run=dry_run)

    try:
        s3 = boto3.client("s3")

        log.info("Loading script from S3: %s", script_s3_key)
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        log.info("Loading profile: %s", profile_name)
        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        # Use the model configured in the profile
        global _active_model_id
        _active_model_id = profile.get("llm", {}).get("script_model", BEDROCK_MODEL_ID_DEFAULT)

        title = script.get("title", "") or title_passthrough
        mood = script.get("mood", "neutral")
        accent_color = profile.get("thumbnail", {}).get("accent_color", "#C8A96E")

        if dry_run:
            log.info("DRY RUN mode — returning stub thumbnail keys")
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "title": title,
                "final_video_s3_key": final_video_s3_key,
                "video_duration_sec": video_duration_sec,
                "thumbnail_s3_keys": [
                    f"{run_id}/thumbnails/thumbnail_0.jpg",
                    f"{run_id}/thumbnails/thumbnail_1.jpg",
                    f"{run_id}/thumbnails/thumbnail_2.jpg",
                ],
                "primary_thumbnail_s3_key": f"{run_id}/thumbnails/thumbnail_0.jpg",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            log.info("Downloading video for keyframe extraction: %s", final_video_s3_key)
            video_local = os.path.join(tmpdir, "final_video.mp4")
            s3.download_file(S3_OUTPUTS_BUCKET, final_video_s3_key, video_local)

            log.info("Extracting keyframes")
            frames = _extract_keyframes(video_local, tmpdir, n=6)

            log.info("Scoring %d frames via Bedrock", len(frames))
            scores = [_score_frame_bedrock(f) for f in frames]
            best_frame_idx = scores.index(max(scores))
            best_frame = frames[best_frame_idx]
            log.info("Best frame: index=%d score=%.2f", best_frame_idx, max(scores))

            log.info("Generating thumbnail concepts")
            concepts = _generate_thumbnail_concepts(title, mood, accent_color)
            if len(concepts) < 3:
                concepts += [concepts[0]] * (3 - len(concepts))

            log.info("Rendering %d thumbnail variants", len(concepts[:3]))
            thumbnail_local_paths = []
            for i, concept in enumerate(concepts[:3]):
                t_path = _render_thumbnail(best_frame, concept, profile, tmpdir, i)
                thumbnail_local_paths.append(t_path)

            log.info("Uploading thumbnails to S3")
            thumbnail_s3_keys = []
            for i, t_path in enumerate(thumbnail_local_paths):
                key = f"{run_id}/thumbnails/thumbnail_{i}.jpg"
                s3.upload_file(t_path, S3_OUTPUTS_BUCKET, key)
                thumbnail_s3_keys.append(key)

        elapsed = time.time() - step_start
        notify_step_complete("thumbnail", run_id, [
            {"name": "Title", "value": title[:100], "inline": False},
            {"name": "Best Score", "value": f"{max(scores) if scores else 0.0:.2f}", "inline": True},
            {"name": "Variants", "value": str(len(thumbnail_s3_keys)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0xF1C40F)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "script_s3_key": script_s3_key,
            "title": title,
            "final_video_s3_key": final_video_s3_key,
            "video_duration_sec": video_duration_sec,
            "thumbnail_s3_keys": thumbnail_s3_keys,
            "primary_thumbnail_s3_key": thumbnail_s3_keys[0],
            "frame_scores": scores,
            "best_frame_score": max(scores) if scores else 0.0,
        }

    except Exception as exc:
        log.error("Thumbnail step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "thumbnail", exc)
        raise
