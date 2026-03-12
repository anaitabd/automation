import json
import os
import time
import boto3

NOVA_REEL_MODEL_ID = os.environ.get("NOVA_REEL_MODEL_ID", "amazon.nova-reel-v1:0")
NOVA_REEL_WIDTH = int(os.environ.get("NOVA_REEL_WIDTH", "1280"))
NOVA_REEL_HEIGHT = int(os.environ.get("NOVA_REEL_HEIGHT", "720"))
NOVA_REEL_FPS = int(os.environ.get("NOVA_REEL_FPS", "24"))
NOVA_REEL_DURATION_SEC = int(os.environ.get("NOVA_REEL_DURATION_SEC", "6"))
NOVA_REEL_POLL_INTERVAL = int(os.environ.get("NOVA_REEL_POLL_INTERVAL", "10"))
NOVA_REEL_POLL_TIMEOUT = int(os.environ.get("NOVA_REEL_POLL_TIMEOUT", "600"))


def _start_generation(
    client,
    text_prompt: str,
    image_s3_uri: str | None,
    output_s3_uri: str,
    duration_seconds: int,
    fps: int,
    width: int,
    height: int,
    seed: int,
) -> str:
    video_generation_config = {
        "durationSeconds": duration_seconds,
        "fps": fps,
        "dimension": f"{width}x{height}",
        "seed": seed,
    }
    model_input: dict = {
        "taskType": "TEXT_VIDEO",
        "textToVideoParams": {
            "text": text_prompt,
        },
        "videoGenerationConfig": video_generation_config,
    }
    if image_s3_uri:
        model_input["taskType"] = "TEXT_IMAGE_TO_VIDEO"
        model_input["textToVideoParams"]["images"] = [
            {"format": "png", "source": {"s3Location": {"uri": image_s3_uri}}}
        ]
    response = client.start_async_invoke(
        modelId=NOVA_REEL_MODEL_ID,
        modelInput=model_input,
        outputDataConfig={"s3OutputDataConfig": {"s3Uri": output_s3_uri}},
    )
    return response["invocationArn"]


def _poll_until_complete(
    client,
    invocation_arn: str,
    poll_interval: int = NOVA_REEL_POLL_INTERVAL,
    timeout: int = NOVA_REEL_POLL_TIMEOUT,
) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status_response = client.get_async_invoke(invocationArn=invocation_arn)
        status = status_response.get("status", "")
        if status == "Completed":
            output_config = status_response.get("outputDataConfig", {})
            s3_config = output_config.get("s3OutputDataConfig", {})
            return s3_config.get("s3Uri", "")
        if status in ("Failed", "Cancelled"):
            failure_message = status_response.get("failureMessage", "unknown error")
            raise RuntimeError(
                f"Nova Reel invocation {invocation_arn} {status}: {failure_message}"
            )
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Nova Reel invocation {invocation_arn} did not complete within {timeout}s"
    )


def generate_video(
    text_prompt: str,
    output_s3_bucket: str,
    output_s3_prefix: str,
    image_s3_uri: str | None = None,
    duration_seconds: int = NOVA_REEL_DURATION_SEC,
    fps: int = NOVA_REEL_FPS,
    width: int = NOVA_REEL_WIDTH,
    height: int = NOVA_REEL_HEIGHT,
    seed: int = 0,
    poll_interval: int = NOVA_REEL_POLL_INTERVAL,
    poll_timeout: int = NOVA_REEL_POLL_TIMEOUT,
) -> str:
    client = boto3.client("bedrock-runtime")
    output_s3_uri = f"s3://{output_s3_bucket}/{output_s3_prefix}"
    invocation_arn = _start_generation(
        client=client,
        text_prompt=text_prompt,
        image_s3_uri=image_s3_uri,
        output_s3_uri=output_s3_uri,
        duration_seconds=duration_seconds,
        fps=fps,
        width=width,
        height=height,
        seed=seed,
    )
    completed_s3_uri = _poll_until_complete(
        client=client,
        invocation_arn=invocation_arn,
        poll_interval=poll_interval,
        timeout=poll_timeout,
    )
    return completed_s3_uri


def generate_and_upload_video(
    text_prompt: str,
    output_s3_key: str,
    output_s3_bucket: str,
    image_s3_uri: str | None = None,
    duration_seconds: int = NOVA_REEL_DURATION_SEC,
    fps: int = NOVA_REEL_FPS,
    width: int = NOVA_REEL_WIDTH,
    height: int = NOVA_REEL_HEIGHT,
    seed: int = 0,
    poll_interval: int = NOVA_REEL_POLL_INTERVAL,
    poll_timeout: int = NOVA_REEL_POLL_TIMEOUT,
) -> str:
    output_prefix = output_s3_key.rstrip("/")
    completed_uri = generate_video(
        text_prompt=text_prompt,
        output_s3_bucket=output_s3_bucket,
        output_s3_prefix=output_prefix,
        image_s3_uri=image_s3_uri,
        duration_seconds=duration_seconds,
        fps=fps,
        width=width,
        height=height,
        seed=seed,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
    )
    s3 = boto3.client("s3")
    completed_bucket = completed_uri.replace("s3://", "").split("/")[0]
    completed_key = "/".join(completed_uri.replace("s3://", "").split("/")[1:])
    video_s3_key = completed_key.rstrip("/") + "/output.mp4" if not completed_key.endswith(".mp4") else completed_key
    final_s3_key = output_s3_key if output_s3_key.endswith(".mp4") else output_s3_key + ".mp4"
    copy_source = {"Bucket": completed_bucket, "Key": video_s3_key}
    s3.copy(copy_source, output_s3_bucket, final_s3_key)
    return final_s3_key
