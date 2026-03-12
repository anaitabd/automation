"""
nova_canvas.py — AWS Bedrock Nova Canvas image generation helper.

Public API:
    generate_image(prompt, width=1280, height=720, quality="standard") -> bytes
        Returns raw PNG bytes for the generated image.

Requires Bedrock IAM access to amazon.nova-canvas-v1:0 in us-east-1.
Supports dimensions: must be multiples of 16, max 4096x4096.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
from typing import Any

import boto3

log = logging.getLogger(__name__)

_REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
_MODEL_ID = "amazon.nova-canvas-v1:0"

_bedrock: Any = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=_REGION)
    return _bedrock


def generate_image(
    prompt: str,
    width: int = 1280,
    height: int = 720,
    quality: str = "standard",
    seed: int | None = None,
) -> bytes:
    """
    Generate an image using Amazon Nova Canvas.

    Args:
        prompt:  Text prompt describing the desired image.
        width:   Image width in pixels (multiple of 16, max 4096).
        height:  Image height in pixels (multiple of 16, max 4096).
        quality: "standard" or "premium".
        seed:    Optional fixed seed for reproducibility (0 = random).

    Returns:
        PNG image as raw bytes.

    Raises:
        Exception on API error or empty response.
    """
    # Clamp dimensions to multiples of 16
    width = max(16, (width // 16) * 16)
    height = max(16, (height // 16) * 16)

    if seed is None:
        seed = random.randint(0, 2_147_483_647)

    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {
            "text": prompt,
        },
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "width": width,
            "height": height,
            "quality": quality,
            "seed": seed,
        },
    }

    client = _get_bedrock()
    response = client.invoke_model(
        modelId=_MODEL_ID,
        body=json.dumps(body),
        accept="application/json",
        contentType="application/json",
    )

    response_body = json.loads(response["body"].read())

    images = response_body.get("images", [])
    if not images:
        raise ValueError("Nova Canvas returned no images")

    png_bytes = base64.b64decode(images[0])
    log.info("Nova Canvas: generated %dx%d image (%d bytes)", width, height, len(png_bytes))
    return png_bytes
