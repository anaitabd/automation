import json
import time
import urllib.error
import urllib.request

import boto3

from nexus_pipeline_utils import get_logger

log = get_logger("nexus-visuals.gpu_client")

_POLL_INTERVAL_SEC = 10
_MAX_POLL_ATTEMPTS = 80
_EC2_BOOT_WAIT_SEC = 60


def start_ec2(instance_id: str) -> None:
    ec2 = boto3.client("ec2")
    ec2.start_instances(InstanceIds=[instance_id])
    waiter = ec2.get_waiter("instance_running")
    for attempt in range(3):
        try:
            waiter.wait(InstanceIds=[instance_id])
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(30)
    log.info("EC2 %s running — waiting %ds for FastAPI boot", instance_id, _EC2_BOOT_WAIT_SEC)
    time.sleep(_EC2_BOOT_WAIT_SEC)


def stop_ec2(instance_id: str) -> None:
    ec2 = boto3.client("ec2")
    ec2.stop_instances(InstanceIds=[instance_id])
    log.info("EC2 %s stop requested", instance_id)


def _http_post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "NexusCloud/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "NexusCloud/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poll_job(server_url: str, job_id: str) -> str:
    status_url = f"{server_url.rstrip('/')}/status/{job_id}"
    for attempt in range(_MAX_POLL_ATTEMPTS):
        time.sleep(_POLL_INTERVAL_SEC)
        try:
            result = _http_get(status_url)
        except Exception as exc:
            log.warning("Poll attempt %d failed: %s", attempt + 1, exc)
            continue
        status = result.get("status", "")
        log.info("Job %s poll %d/%d: status=%s", job_id, attempt + 1, _MAX_POLL_ATTEMPTS, status)
        if status == "complete":
            s3_key = result.get("s3_key")
            if not s3_key:
                raise RuntimeError(f"Job {job_id} complete but no s3_key in response")
            return s3_key
        if status == "failed":
            raise RuntimeError(f"Wan job {job_id} failed: {result.get('error', 'unknown')}")
    raise TimeoutError(f"Wan job {job_id} timed out after {_MAX_POLL_ATTEMPTS * _POLL_INTERVAL_SEC}s")


def generate_clip(
    server_url: str,
    prompt: str,
    image_s3_key: str | None,
    duration_sec: float,
    run_id: str,
    section_idx: int,
) -> str:
    generate_url = f"{server_url.rstrip('/')}/generate"
    payload: dict = {
        "prompt": prompt,
        "duration_sec": duration_sec,
        "run_id": run_id,
        "section_idx": section_idx,
        "mode": "i2v" if image_s3_key else "t2v",
    }
    if image_s3_key:
        payload["image_s3_key"] = image_s3_key

    for attempt in range(2):
        try:
            response = _http_post(generate_url, payload)
            job_id = response.get("job_id")
            if not job_id:
                raise ValueError(f"No job_id in response: {response}")
            log.info("Wan job submitted: job_id=%s section=%d", job_id, section_idx)
            return _poll_job(server_url, job_id)
        except (TimeoutError, RuntimeError):
            raise
        except Exception as exc:
            if attempt == 1:
                raise RuntimeError(f"Failed to submit Wan job after 2 attempts: {exc}") from exc
            log.warning("Wan job submit attempt %d failed: %s — retrying", attempt + 1, exc)
            time.sleep(5)
    raise RuntimeError("Unreachable")
