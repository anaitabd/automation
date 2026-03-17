import base64
import json
import logging
import os
import time
import boto3

NOVA_CANVAS_MODEL_ID = os.environ.get("NOVA_CANVAS_MODEL_ID", "amazon.nova-canvas-v1:0")
NOVA_CANVAS_WIDTH = int(os.environ.get("NOVA_CANVAS_WIDTH", "1280"))
NOVA_CANVAS_HEIGHT = int(os.environ.get("NOVA_CANVAS_HEIGHT", "720"))
NOVA_CANVAS_QUALITY = os.environ.get("NOVA_CANVAS_QUALITY", "standard")
NOVA_CANVAS_CFG_SCALE = float(os.environ.get("NOVA_CANVAS_CFG_SCALE", "8.0"))
NOVA_CANVAS_SEED = int(os.environ.get("NOVA_CANVAS_SEED", "0"))

_log = logging.getLogger(__name__)


def generate_image(
    prompt: str,
    negative_prompt: str = "blurry, low quality, distorted, text, watermark",
    width: int = NOVA_CANVAS_WIDTH,
    height: int = NOVA_CANVAS_HEIGHT,
    quality: str = NOVA_CANVAS_QUALITY,
    cfg_scale: float = NOVA_CANVAS_CFG_SCALE,
    seed: int = NOVA_CANVAS_SEED,
    retries: int = 3,
) -> bytes:
    client = boto3.client("bedrock-runtime")
    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {
            "text": prompt,
            "negativeText": negative_prompt,
        },
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "width": width,
            "height": height,
            "quality": quality,
            "cfgScale": cfg_scale,
            "seed": seed,
        },
    }
    for attempt in range(retries):
        try:
            response = client.invoke_model(
                modelId=NOVA_CANVAS_MODEL_ID,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(response["body"].read())
            images = result.get("images", [])
            if not images:
                raise RuntimeError("Nova Canvas returned no images")
            return base64.b64decode(images[0])
        except Exception as exc:
            if attempt == retries - 1:
                raise
            _log.warning(
                "nova_canvas.generate_image attempt %d/%d failed: %s",
                attempt + 1, retries, exc,
            )
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def generate_and_upload_image(
    prompt: str,
    s3_key: str,
    bucket: str,
    negative_prompt: str = "blurry, low quality, distorted, text, watermark",
    width: int = NOVA_CANVAS_WIDTH,
    height: int = NOVA_CANVAS_HEIGHT,
    quality: str = NOVA_CANVAS_QUALITY,
    cfg_scale: float = NOVA_CANVAS_CFG_SCALE,
    seed: int = NOVA_CANVAS_SEED,
) -> str:
    image_bytes = generate_image(
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        quality=quality,
        cfg_scale=cfg_scale,
        seed=seed,
    )
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=image_bytes,
        ContentType="image/png",
    )
    return s3_key
