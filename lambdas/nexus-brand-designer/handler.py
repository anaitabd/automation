"""nexus-brand-designer — Claude-powered brand kit generation.

Given a channel name, niche, profile, and style hints, uses Claude Sonnet
to generate a complete brand kit: colors, font, LUT preset.
Also selects an appropriate ElevenLabs voice_id from the profile defaults.
"""

import json
import logging
import os

import boto3

from nexus_pipeline_utils import get_logger

log = get_logger("nexus-brand-designer")

_cache: dict = {}

CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")
BRAND_MODEL_ID = os.environ.get(
    "BRAND_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"
)

# Available LUT presets (from luts_generated/)
AVAILABLE_LUTS = [
    "cinematic_teal_orange",
    "cold_blue_corporate",
    "high_contrast",
    "punchy_vibrant_warm",
    "vintage_sepia",
]

# Font options
AVAILABLE_FONTS = [
    "Cinzel",
    "Roboto Bold",
    "Impact",
    "Playfair Display",
    "Montserrat Bold",
    "Oswald",
    "Lora",
    "Bebas Neue",
]


def _load_profile(profile: str) -> dict:
    """Load the profile JSON from CONFIG_BUCKET, cached."""
    cache_key = f"profile_{profile}"
    if cache_key not in _cache:
        s3 = boto3.client("s3")
        try:
            resp = s3.get_object(Bucket=CONFIG_BUCKET, Key=f"profiles/{profile}.json")
            _cache[cache_key] = json.loads(resp["Body"].read())
        except Exception as exc:
            log.warning("Failed to load profile %s: %s — using defaults", profile, exc)
            _cache[cache_key] = {}
    return _cache[cache_key]


def _invoke_claude(prompt: str, max_tokens: int = 1024) -> str:
    """Call Claude via Bedrock and return the text response."""
    client = boto3.client("bedrock-runtime")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    response = client.invoke_model(
        modelId=BRAND_MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    # Claude returns content as a list of blocks
    content = result.get("content", [])
    text_parts = [block["text"] for block in content if block.get("type") == "text"]
    return "\n".join(text_parts)


def _parse_brand_json(text: str) -> dict:
    """Extract JSON from Claude's response (may be wrapped in markdown)."""
    # Try to find JSON block
    import re
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(1))
    # Try bare JSON
    brace_start = text.find("{")
    brace_end = text.rfind("}") + 1
    if brace_start >= 0 and brace_end > brace_start:
        return json.loads(text[brace_start:brace_end])
    raise ValueError(f"Could not parse brand JSON from response: {text[:200]}")


def lambda_handler(event: dict, context) -> dict:
    channel_id = event["channel_id"]
    channel_name = event["channel_name"]
    niche = event["niche"]
    profile = event.get("profile", "documentary")
    style_hints = event.get("style_hints", "")

    log.info("Brand design starting for '%s' (niche=%s, profile=%s)", channel_name, niche, profile)

    # Load profile defaults for voice and thumbnail
    profile_data = _load_profile(profile)
    default_voice_id = profile_data.get("voice", {}).get("voice_id", "21m00Tcm4TlvDq8ikWAM")
    default_font = profile_data.get("thumbnail", {}).get("font", "Cinzel")
    default_lut = profile_data.get("shorts", {}).get("lut_preset", "cinematic_teal_orange")

    prompt = f"""You are a YouTube brand strategist and visual designer. Generate a complete brand kit for a new YouTube channel.

Channel Name: {channel_name}
Niche/Topic: {niche}
Content Profile: {profile}
Style Hints: {style_hints or "No specific hints — use best judgment based on niche and profile."}

Generate a JSON object with these exact keys:
- "primary_color": hex color (main brand color, used in thumbnails, overlays)
- "secondary_color": hex color (background/secondary, usually darker)  
- "accent_color": hex color (highlights, CTAs, emphasis)
- "font": one of {json.dumps(AVAILABLE_FONTS)} — pick what best matches the channel's personality
- "lut_preset": one of {json.dumps(AVAILABLE_LUTS)} — pick the color grading LUT that best fits the vibe
- "tagline": a short 3–8 word channel tagline
- "thumbnail_style": brief description of ideal thumbnail aesthetic (2–3 sentences)
- "brand_personality": 3 adjective words describing the brand voice

Rules:
- Colors must have good contrast (primary vs secondary should be readable)
- Secondary color should be dark enough for backgrounds (#1A-#2A range for dark themes)
- Accent should pop against both primary and secondary
- Consider the niche and profile when selecting colors (finance = professional/blue-green, documentary = cinematic/warm, entertainment = vibrant/punchy)

Return ONLY valid JSON, no markdown, no explanation."""

    try:
        response_text = _invoke_claude(prompt)
        brand = _parse_brand_json(response_text)
        log.info("Claude brand response: %s", json.dumps(brand)[:500])
    except Exception as exc:
        log.warning("Claude brand design failed: %s — using defaults", exc)
        brand = {
            "primary_color": "#4F6EF7",
            "secondary_color": "#1A1A2E",
            "accent_color": "#FFD700",
            "font": default_font,
            "lut_preset": default_lut,
            "tagline": f"Explore {niche}",
            "thumbnail_style": "Clean, professional style with bold text overlays",
            "brand_personality": ["informative", "engaging", "professional"],
        }

    # Validate / sanitize
    if brand.get("font") not in AVAILABLE_FONTS:
        brand["font"] = default_font
    if brand.get("lut_preset") not in AVAILABLE_LUTS:
        brand["lut_preset"] = default_lut

    # Ensure hex colors
    for key in ("primary_color", "secondary_color", "accent_color"):
        val = brand.get(key, "")
        if not val.startswith("#") or len(val) not in (4, 7):
            brand[key] = {"primary_color": "#4F6EF7", "secondary_color": "#1A1A2E", "accent_color": "#FFD700"}[key]

    # Upload brand kit to S3 for reference
    s3 = boto3.client("s3")
    brand_key = f"channels/{channel_id}/brand.json"
    s3.put_object(
        Bucket=CONFIG_BUCKET,
        Key=brand_key,
        Body=json.dumps(brand, indent=2),
        ContentType="application/json",
    )

    return {
        "channel_id": channel_id,
        "brand": brand,
        "voice_id": default_voice_id,
        "brand_s3_key": brand_key,
    }

