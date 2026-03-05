#!/usr/bin/env python3
"""
resume_run.py — Resume a failed Nexus pipeline run from where it left off.

Checks S3 for completed step artifacts, determines the next step to run,
reads metadata from existing S3 files, then starts a new Step Functions
execution with the resume_from field and all required state pre-populated.

Usage:
    python scripts/resume_run.py <run_id> [--from STEP] [--dry-run]

Examples:
    python scripts/resume_run.py e0e1d1ab-e575-4a95-8b72-babd36653832
    python scripts/resume_run.py e0e1d1ab-e575-4a95-8b72-babd36653832 --from Editor
    python scripts/resume_run.py e0e1d1ab-e575-4a95-8b72-babd36653832 --from Thumbnail --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load .env from project root
_REPO_ROOT = Path(__file__).parent.parent
load_dotenv(_REPO_ROOT / ".env")

# ── AWS clients ───────────────────────────────────────────────────────────────
s3 = boto3.client(
    "s3",
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
sfn = boto3.client(
    "stepfunctions",
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
lam = boto3.client(
    "lambda",
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)

STATE_MACHINE_ARN = os.environ.get(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline",
)
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets-670294435884")
OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")

# Pipeline step order
STEPS = ["Research", "Script", "AudioVisuals", "Editor", "Thumbnail", "Notify"]


def _s3_exists(bucket: str, key: str) -> bool:
    """Return True if the S3 object exists."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def _s3_read_json(bucket: str, key: str) -> dict:
    """Download and parse a JSON file from S3."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except ClientError as e:
        raise RuntimeError(f"Cannot read s3://{bucket}/{key}: {e}") from e


def _detect_completed_steps(run_id: str) -> list[str]:
    """
    Check S3 for artifacts of each step and return the list of completed steps.
    S3 key layout:
      {run_id}/research.json          → Research done
      {run_id}/script.json            → Script done
      {run_id}/audio/mixed_audio.wav  → Audio done  ─┐ AudioVisuals
      {run_id}/status/visuals_sections.json → Visuals done ─┘
      {run_id}/review/final_video.mp4 → Editor done
      {run_id}/review/thumbnail_0.jpg → Thumbnail done
    """
    completed = []

    if _s3_exists(ASSETS_BUCKET, f"{run_id}/research.json"):
        completed.append("Research")

    if _s3_exists(ASSETS_BUCKET, f"{run_id}/script.json"):
        completed.append("Script")

    audio_done = _s3_exists(ASSETS_BUCKET, f"{run_id}/audio/mixed_audio.wav")
    visuals_done = _s3_exists(ASSETS_BUCKET, f"{run_id}/status/visuals_sections.json")
    if audio_done or visuals_done:
        completed.append("AudioVisuals")

    if _s3_exists(OUTPUTS_BUCKET, f"{run_id}/review/final_video.mp4"):
        completed.append("Editor")

    if _s3_exists(OUTPUTS_BUCKET, f"{run_id}/review/thumbnail_0.jpg") or \
       _s3_exists(OUTPUTS_BUCKET, f"{run_id}/review/thumbnail.jpg"):
        completed.append("Thumbnail")

    return completed


def _next_step_after(completed: list[str]) -> str:
    """Return the first step not in completed, in pipeline order."""
    for step in STEPS:
        if step not in completed:
            return step
    return "Notify"


def _read_script_meta(run_id: str) -> dict:
    """Read script.json and return relevant metadata."""
    data = _s3_read_json(OUTPUTS_BUCKET, f"{run_id}/script.json")
    return {
        "script_s3_key": f"{run_id}/script.json",
        "title": data.get("title", ""),
        "section_count": data.get("section_count", len(data.get("sections", []))),
        "total_duration_estimate": data.get("total_duration_estimate", 600),
    }


def _read_research_meta(run_id: str) -> dict:
    """Read research.json and return relevant metadata."""
    data = _s3_read_json(OUTPUTS_BUCKET, f"{run_id}/research.json")
    return {
        "research_s3_key": f"{run_id}/research.json",
        "selected_topic": data.get("selected_topic", ""),
        "angle": data.get("angle", ""),
        "trending_context": data.get("trending_context", ""),
    }


def _get_subnets() -> list[str]:
    """Fetch subnet IDs from the nexus-api-handler Lambda env var."""
    try:
        resp = lam.get_function_configuration(FunctionName="nexus-api-handler")
        raw = resp.get("Environment", {}).get("Variables", {}).get("ECS_SUBNETS", "[]")
        return json.loads(raw)
    except Exception as e:
        print(f"[WARN] Could not fetch subnets automatically: {e}")
        print("      Set ECS_SUBNETS in .env or provide them manually.")
        return []


def _get_niche_profile_from_script(run_id: str) -> tuple[str, str, bool]:
    """Try to read niche/profile/dry_run from script.json metadata."""
    try:
        data = _s3_read_json(OUTPUTS_BUCKET, f"{run_id}/script.json")
        return (
            data.get("niche", ""),
            data.get("profile", "documentary"),
            bool(data.get("dry_run", False)),
        )
    except Exception:
        return "", "documentary", False


def build_resume_input(
    run_id: str,
    resume_from: str,
    niche: str = "",
    profile: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build the full Step Functions input payload for resuming at resume_from."""

    subnets = _get_subnets()
    if not subnets:
        raise RuntimeError(
            "No subnets found. Set ECS_SUBNETS in .env or ensure nexus-api-handler Lambda exists."
        )

    # Try to fill niche/profile from script metadata if not provided
    if not niche:
        niche, profile_from_s3, dry_run_from_s3 = _get_niche_profile_from_script(run_id)
        if not profile:
            profile = profile_from_s3
        if not dry_run:
            dry_run = dry_run_from_s3

    base: dict[str, Any] = {
        "run_id": run_id,
        "niche": niche,
        "profile": profile,
        "dry_run": dry_run,
        "subnets": subnets,
        "resume_from": resume_from,
    }

    if resume_from == "Research":
        # Fresh start — no extra fields needed
        pass

    elif resume_from == "Script":
        base.update(_read_research_meta(run_id))

    elif resume_from == "AudioVisuals":
        script_meta = _read_script_meta(run_id)
        base.update(_read_research_meta(run_id))
        base.update(script_meta)

    elif resume_from == "Editor":
        script_meta = _read_script_meta(run_id)
        base.update(script_meta)
        base["mixed_audio_s3_key"] = f"{run_id}/audio/mixed_audio.wav"
        # Verify audio exists (stored in assets bucket)
        if not _s3_exists(ASSETS_BUCKET, f"{run_id}/audio/mixed_audio.wav"):
            print(f"[WARN] mixed_audio.wav not found in S3 for run {run_id} — Editor may fail.")

    elif resume_from == "Thumbnail":
        script_meta = _read_script_meta(run_id)
        base.update(script_meta)
        base["final_video_s3_key"] = f"{run_id}/review/final_video.mp4"
        base["video_duration_sec"] = script_meta.get("total_duration_estimate", 600)

    elif resume_from == "Notify":
        script_meta = _read_script_meta(run_id)
        base.update(script_meta)
        base["final_video_s3_key"] = f"{run_id}/review/final_video.mp4"
        base["video_duration_sec"] = script_meta.get("total_duration_estimate", 600)
        # Try to find thumbnail keys
        for i in range(3):
            key = f"{run_id}/review/thumbnail_{i}.jpg"
            if _s3_exists(OUTPUTS_BUCKET, key):
                base.setdefault("thumbnail_s3_keys", []).append(key)
        if "thumbnail_s3_keys" not in base:
            base["thumbnail_s3_keys"] = []
        base["primary_thumbnail_s3_key"] = (
            base["thumbnail_s3_keys"][0] if base["thumbnail_s3_keys"] else ""
        )

    else:
        raise ValueError(f"Unknown resume_from step: {resume_from!r}. "
                         f"Valid steps: {STEPS}")

    return base


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume a failed Nexus pipeline run from a specific step."
    )
    parser.add_argument("run_id", help="The run_id (e.g. e0e1d1ab-e575-4a95-8b72-babd36653832)")
    parser.add_argument(
        "--from", dest="resume_from", default=None,
        choices=STEPS,
        help="Step to resume from. Auto-detected from S3 if not specified.",
    )
    parser.add_argument("--niche", default="", help="Override niche (auto-read from S3 if omitted)")
    parser.add_argument("--profile", default="", help="Override profile (auto-read from S3 if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Dry run — don't actually start execution")
    args = parser.parse_args()

    run_id: str = args.run_id
    print(f"\n🔍 Checking S3 artifacts for run: {run_id}")

    completed = _detect_completed_steps(run_id)
    print(f"   Completed steps : {completed or ['(none)']}")

    resume_from = args.resume_from
    if resume_from is None:
        resume_from = _next_step_after(completed)
        print(f"   Auto-detected   : resume from '{resume_from}'")
    else:
        print(f"   Forced          : resume from '{resume_from}'")

    if resume_from == "Notify" and "Thumbnail" in completed:
        print("✅ Pipeline appears fully complete — nothing to resume.")
        sys.exit(0)

    print(f"\n📦 Building input payload for '{resume_from}'...")
    try:
        payload = build_resume_input(
            run_id=run_id,
            resume_from=resume_from,
            niche=args.niche,
            profile=args.profile,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"❌ Failed to build payload: {e}")
        sys.exit(1)

    print(f"   Payload preview:")
    preview = {k: (v if not isinstance(v, list) or len(str(v)) < 80 else f"[{len(v)} items]")
               for k, v in payload.items()}
    for k, v in preview.items():
        print(f"     {k}: {v}")

    if args.dry_run:
        print("\n[DRY RUN] Would start execution with the above payload.")
        print(json.dumps(payload, indent=2))
        sys.exit(0)

    import uuid
    exec_name = f"resume-{resume_from.lower()}-{run_id[:8]}-{str(uuid.uuid4())[:8]}"
    print(f"\n🚀 Starting new execution: {exec_name}")
    try:
        resp = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(payload),
        )
        exec_arn = resp["executionArn"]
        print(f"✅ Started: {exec_arn}")
        print(f"\n   Monitor with:")
        print(f"   aws stepfunctions get-execution-history \\")
        print(f"     --execution-arn '{exec_arn}' \\")
        print(f"     --reverse-order --max-items 5 --no-cli-pager")
    except Exception as e:
        print(f"❌ Failed to start execution: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
