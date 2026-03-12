"""
nova_reel.py — AWS Bedrock Nova Reel async video generation helper.

Public API:
    submit_and_poll_batch(jobs, output_s3_prefix, timeout_sec, max_clips)
        -> dict[int, str | None]   # section_idx -> S3 URI or None on failure

Each job dict: {"section_idx": int, "prompt": str}
Each clip is 6 s at 1280×720 24 fps.

Requires Bedrock IAM access to amazon.nova-reel-v1:0 in us-east-1.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import boto3

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
_MODEL_ID = "amazon.nova-reel-v1:0"
_DURATION_SEC = 6
_FPS = 24
_DIMENSION = "1280x720"
_DEFAULT_TIMEOUT = 600   # seconds
_POLL_INTERVAL = 15      # seconds between status checks

# ---------------------------------------------------------------------------
# Bedrock client (lazily created so tests can patch boto3)
# ---------------------------------------------------------------------------
_bedrock: Any = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=_REGION)
    return _bedrock


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _submit_one(prompt: str, output_s3_uri: str) -> str:
    """
    Submit a single async Nova Reel job.
    Returns the invocation ARN.

    output_s3_uri must be an S3 URI prefix like s3://bucket/prefix/
    (Bedrock will write the MP4 under that prefix).
    """
    client = _get_bedrock()
    body = {
        "taskType": "TEXT_VIDEO",
        "textToVideoParams": {
            "text": prompt,
        },
        "videoGenerationConfig": {
            "durationSeconds": _DURATION_SEC,
            "fps": _FPS,
            "dimension": _DIMENSION,
            "seed": 0,
        },
    }
    resp = client.start_async_invoke(
        modelId=_MODEL_ID,
        modelInput=body,
        outputDataConfig={
            "s3OutputDataConfig": {
                "s3Uri": output_s3_uri,
            }
        },
    )
    return resp["invocationArn"]


def _poll_one(invocation_arn: str, deadline: float) -> str | None:
    """
    Poll a single Nova Reel job until completed or deadline exceeded.
    Returns the S3 URI of the generated MP4, or None on failure/timeout.
    """
    client = _get_bedrock()
    while time.time() < deadline:
        try:
            resp = client.get_async_invoke(invocationArn=invocation_arn)
        except Exception as exc:
            log.warning("Nova Reel poll error for %s: %s", invocation_arn[-12:], exc)
            time.sleep(_POLL_INTERVAL)
            continue

        status = resp.get("status", "")
        if status == "Completed":
            # Output location is under the prefix we supplied
            out = resp.get("outputDataConfig", {}).get("s3OutputDataConfig", {})
            s3_prefix = out.get("s3Uri", "")
            # Bedrock writes a folder like <prefix>/<jobId>/output.mp4
            # The API also provides the output location in the response
            # Try to find it from the response first
            if s3_prefix:
                # The model writes output.mp4 under the prefix
                if not s3_prefix.endswith("/"):
                    s3_prefix += "/"
                # Parse bucket + key from s3_prefix
                parts = s3_prefix.replace("s3://", "").split("/", 1)
                bucket = parts[0]
                key_prefix = parts[1] if len(parts) > 1 else ""
                # Find the output .mp4 in S3
                s3 = boto3.client("s3", region_name=_REGION)
                try:
                    list_resp = s3.list_objects_v2(Bucket=bucket, Prefix=key_prefix, MaxKeys=10)
                    for obj in list_resp.get("Contents", []):
                        if obj["Key"].endswith(".mp4"):
                            return f"s3://{bucket}/{obj['Key']}"
                except Exception as list_err:
                    log.warning("Nova Reel: S3 list error: %s", list_err)
                # Fallback: return the prefix URI itself
                return s3_prefix
            return None

        elif status in ("Failed", "failed"):
            fail_msg = resp.get("failureMessage", "unknown")
            log.warning("Nova Reel job %s failed: %s", invocation_arn[-12:], fail_msg)
            return None

        # InProgress or Submitted — keep waiting
        log.debug("Nova Reel job %s: %s (%.0fs left)", invocation_arn[-12:], status,
                  deadline - time.time())
        time.sleep(_POLL_INTERVAL)

    log.warning("Nova Reel job %s timed out", invocation_arn[-12:])
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit_and_poll_batch(
    jobs: list[dict],
    output_s3_prefix: str,
    timeout_sec: int = _DEFAULT_TIMEOUT,
    max_clips: int | None = None,
) -> dict[int, str | None]:
    """
    Submit multiple Nova Reel jobs concurrently and poll until all finish
    or ``timeout_sec`` elapses.

    Args:
        jobs:             List of {"section_idx": int, "prompt": str}.
        output_s3_prefix: S3 prefix (e.g. "s3://bucket/run_id/reel/").
                          Each job gets its own sub-prefix.
        timeout_sec:      Max seconds to wait for ALL jobs (default 600).
        max_clips:        How many clips to actually generate (None = all).

    Returns:
        dict mapping section_idx -> s3_uri string (or None on failure).
    """
    if not jobs:
        return {}

    if max_clips is not None:
        jobs = jobs[:max_clips]

    deadline = time.time() + timeout_sec

    if not output_s3_prefix.endswith("/"):
        output_s3_prefix += "/"

    # ── Step 1: submit all jobs ────────────────────────────────────────────
    arns: dict[int, str] = {}  # section_idx -> invocation_arn
    submit_errors: dict[int, str] = {}

    def _submit_job(job: dict) -> tuple[int, str | None]:
        idx = job["section_idx"]
        prompt = job["prompt"]
        per_job_prefix = f"{output_s3_prefix}sec{idx:03d}/"
        try:
            arn = _submit_one(prompt, per_job_prefix)
            log.info("Nova Reel: submitted job %d (arn …%s)", idx, arn[-12:])
            return idx, arn
        except Exception as exc:
            log.warning("Nova Reel: submit failed for section %d: %s", idx, exc)
            return idx, None

    workers = min(len(jobs), 5)  # stay within Bedrock concurrency limits
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_submit_job, job) for job in jobs]
        for fut in as_completed(futs):
            idx, arn = fut.result()
            if arn:
                arns[idx] = arn
            else:
                submit_errors[idx] = "submit_failed"

    log.info("Nova Reel: %d/%d jobs submitted successfully", len(arns), len(jobs))

    if not arns:
        return {job["section_idx"]: None for job in jobs}

    # ── Step 2: poll all submitted jobs ───────────────────────────────────
    results: dict[int, str | None] = {idx: None for idx in submit_errors}

    def _poll_job(item: tuple[int, str]) -> tuple[int, str | None]:
        idx, arn = item
        uri = _poll_one(arn, deadline)
        return idx, uri

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_poll_job, item) for item in arns.items()]
        for fut in as_completed(futs):
            idx, uri = fut.result()
            results[idx] = uri
            if uri:
                log.info("Nova Reel: section %d → %s", idx, uri)
            else:
                log.warning("Nova Reel: section %d produced no output", idx)

    return results
