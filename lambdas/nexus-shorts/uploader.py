"""S3 multipart upload + manifest.json writer."""

from __future__ import annotations

import json
import os
import time

import boto3

from config import S3_OUTPUTS_BUCKET, SHORTS_OUTPUT_PREFIX


def upload_short(
    local_path: str,
    run_id: str,
    short_id: str,
    tier: str,
) -> str:
    """Upload a short MP4 to S3 using multipart upload for large files.

    Returns the S3 key.
    """
    s3 = boto3.client("s3")
    s3_key = f"{run_id}/{SHORTS_OUTPUT_PREFIX}short_{tier}_{short_id.split('_')[-1]}.mp4"

    file_size = os.path.getsize(local_path)

    if file_size > 50 * 1024 * 1024:  # > 50MB — use multipart
        _multipart_upload(s3, local_path, s3_key, file_size)
    else:
        s3.upload_file(local_path, S3_OUTPUTS_BUCKET, s3_key)

    return s3_key


def _multipart_upload(
    s3, local_path: str, s3_key: str, file_size: int,
    part_size: int = 10 * 1024 * 1024,
) -> None:
    """Upload using S3 multipart API."""
    mpu = s3.create_multipart_upload(
        Bucket=S3_OUTPUTS_BUCKET,
        Key=s3_key,
        ContentType="video/mp4",
    )
    upload_id = mpu["UploadId"]
    parts: list[dict] = []

    try:
        with open(local_path, "rb") as f:
            part_num = 1
            while True:
                data = f.read(part_size)
                if not data:
                    break
                resp = s3.upload_part(
                    Bucket=S3_OUTPUTS_BUCKET,
                    Key=s3_key,
                    UploadId=upload_id,
                    PartNumber=part_num,
                    Body=data,
                )
                parts.append({"PartNumber": part_num, "ETag": resp["ETag"]})
                part_num += 1

        s3.complete_multipart_upload(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=s3_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        s3.abort_multipart_upload(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=s3_key,
            UploadId=upload_id,
        )
        raise


def write_manifest(
    run_id: str,
    channel_id: str,
    shorts_results: list[dict],
) -> str:
    """Write manifest.json with batch results and presigned URLs."""
    s3 = boto3.client("s3")

    succeeded = [r for r in shorts_results if r.get("status") == "success"]
    failed = [r for r in shorts_results if r.get("status") == "failed"]

    # Generate presigned URLs for successful shorts
    for result in succeeded:
        if result.get("s3_key"):
            try:
                result["presigned_url"] = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_OUTPUTS_BUCKET, "Key": result["s3_key"]},
                    ExpiresIn=86400,  # 24h
                )
            except Exception:
                result["presigned_url"] = ""

    manifest = {
        "run_id": run_id,
        "channel_id": channel_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shorts": shorts_results,
        "summary": {
            "total": len(shorts_results),
            "succeeded": len(succeeded),
            "failed": len(failed),
        },
    }

    manifest_key = f"{run_id}/{SHORTS_OUTPUT_PREFIX}manifest.json"
    s3.put_object(
        Bucket=S3_OUTPUTS_BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    return manifest_key


def write_error(
    run_id: str,
    short_id: str,
    error: str,
) -> None:
    """Write per-short error JSON to S3."""
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/{SHORTS_OUTPUT_PREFIX}errors/{short_id}.json",
            Body=json.dumps({
                "short_id": short_id,
                "error": error,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass

