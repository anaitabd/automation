import base64
import json
import logging
import os
import random
import threading
import time
import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

NOVA_CANVAS_MODEL_ID = os.environ.get("NOVA_CANVAS_MODEL_ID", "amazon.nova-canvas-v1:0")
NOVA_CANVAS_WIDTH = int(os.environ.get("NOVA_CANVAS_WIDTH", "1280"))
NOVA_CANVAS_HEIGHT = int(os.environ.get("NOVA_CANVAS_HEIGHT", "720"))
NOVA_CANVAS_QUALITY = os.environ.get("NOVA_CANVAS_QUALITY", "standard")
NOVA_CANVAS_CFG_SCALE = float(os.environ.get("NOVA_CANVAS_CFG_SCALE", "8.0"))
NOVA_CANVAS_SEED = int(os.environ.get("NOVA_CANVAS_SEED", "0"))

bedrock_client = boto3.client("bedrock-runtime")
bedrock_semaphore = threading.Semaphore(4)


def invoke_with_backoff(client, payload: dict, run_id: str = "", max_retries: int = 5) -> dict:
    """Invoke Bedrock invoke_model with semaphore + exponential backoff on ThrottlingException."""
    for attempt in range(max_retries):
        try:
            with bedrock_semaphore:
                return client.invoke_model(**payload)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ThrottlingException" and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                log.warning(
                    "nova_canvas: throttled attempt %d/%d — retrying in %.2fs",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise


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
    payload = {
        "modelId": NOVA_CANVAS_MODEL_ID,
        "body": json.dumps(body),
        "contentType": "application/json",
        "accept": "application/json",
    }
    response = invoke_with_backoff(bedrock_client, payload)
    result = json.loads(response["body"].read())
    images = result.get("images", [])
    if not images:
        raise RuntimeError("Nova Canvas returned no images")
    return base64.b64decode(images[0])


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
