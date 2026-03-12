"""nexus-shorts — Fargate entrypoint.

Generates a batch of vertical short-form MP4s (15s/30s/45s/60s) from the same
script and brand kit used for the long-form video.

Follows existing handler patterns: _cache, get_secret, notify_step_start/complete,
_write_error, state key preservation (run_id, profile, dry_run).
"""

from __future__ import annotations

import json
import os
import sys
import time

import boto3

from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

from config import (
    S3_ASSETS_BUCKET, S3_CONFIG_BUCKET, S3_OUTPUTS_BUCKET,
    SHORTS_ENABLED, SHORTS_TIERS, TIER_DEFS,
)
from broll_fetcher import submit_nova_reel_jobs
from batch_processor import process_batch
from uploader import write_manifest

log = get_logger("nexus-shorts")

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


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


def lambda_handler(event: dict, context=None) -> dict:
    """Main handler — invoked by ECS Fargate or locally via docker compose."""
    run_id: str = event.get("run_id") or os.environ.get("RUN_ID", "")
    profile_name: str = event.get("profile") or os.environ.get("PROFILE", "documentary")
    niche: str = event.get("niche") or os.environ.get("NICHE", "")
    script_s3_key: str = event.get("script_s3_key") or os.environ.get("SCRIPT_S3_KEY", "")
    channel_id: str = event.get("channel_id") or os.environ.get("CHANNEL_ID", "")
    mixed_audio_s3_key: str = event.get("mixed_audio_s3_key") or os.environ.get("MIXED_AUDIO_S3_KEY", "")
    dry_run_raw = event.get("dry_run") if "dry_run" in event else os.environ.get("DRY_RUN", "false")
    dry_run: bool = dry_run_raw if isinstance(dry_run_raw, bool) else str(dry_run_raw).lower() == "true"

    generate_shorts = event.get("generate_shorts", True)
    tiers_requested = event.get("shorts_tiers") or event.get("tiers_requested") or SHORTS_TIERS

    # Ensure tiers_requested is a list
    if isinstance(tiers_requested, str):
        tiers_requested = [t.strip() for t in tiers_requested.split(",")]

    step_start = notify_step_start(
        "shorts", run_id, niche=niche, profile=profile_name, dry_run=dry_run
    )

    try:
        if not SHORTS_ENABLED or not generate_shorts:
            log.info("Shorts disabled — returning empty result")
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": dry_run,
                "shorts_enabled": False,
                "manifest_s3_key": "",
                "shorts_count": 0,
            }

        s3 = boto3.client("s3")

        # Load script
        log.info("Loading script from s3://%s/%s", S3_OUTPUTS_BUCKET, script_s3_key)
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        # Load profile
        log.info("Loading profile: %s", profile_name)
        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        # Build brand kit from profile
        thumbnail_cfg = profile.get("thumbnail", {})
        brand_kit = {
            "accent_color": thumbnail_cfg.get("accent_color", "#C8A96E"),
            "primary_color": thumbnail_cfg.get("accent_color", "#C8A96E"),
            "secondary_color": "#1a1a2e",
            "font": thumbnail_cfg.get("font", "Cinzel"),
            "logo_s3_key": profile.get("logo_s3_key"),
        }

        # Filter to valid tiers
        valid_tiers = [t for t in tiers_requested if t in TIER_DEFS]
        if not valid_tiers:
            log.warning("No valid tiers requested: %s", tiers_requested)
            valid_tiers = ["short"]

        log.info("Processing %d tiers: %s (dry_run=%s)", len(valid_tiers), valid_tiers, dry_run)

        # Submit all Nova Reel jobs in parallel at batch start
        nova_invocations: dict[str, str] = {}
        if not dry_run:
            log.info("Submitting Nova Reel jobs for all clip slots")
            clip_requests: list[dict] = []
            sections = script.get("sections", script.get("scenes", []))
            shorts_cfg = profile.get("shorts", {})
            prompt_template = shorts_cfg.get(
                "nova_reel_prompt_template",
                "vertical cinematic 9:16, {subject}, smooth camera motion, 6 second clip"
            )

            clip_idx = 0
            for tier in valid_tiers:
                td = TIER_DEFS[tier]
                for ci in range(td["nova_clips"]):
                    sec_idx = ci % max(1, len(sections))
                    sec = sections[sec_idx] if sections else {}
                    subject = sec.get("title", niche or "cinematic scene")
                    clip_requests.append({
                        "clip_id": f"short_{tier}_001_clip{clip_idx:02d}",
                        "prompt": prompt_template.format(subject=subject),
                    })
                    clip_idx += 1

            nova_invocations = submit_nova_reel_jobs(clip_requests, run_id, profile)
            log.info("Submitted %d Nova Reel jobs", len(nova_invocations))

        # Process all tiers
        results = process_batch(
            tiers_requested=valid_tiers,
            script=script,
            profile=profile,
            profile_name=profile_name,
            run_id=run_id,
            brand_kit=brand_kit,
            nova_invocations=nova_invocations,
            mixed_audio_s3_key=mixed_audio_s3_key,
            dry_run=dry_run,
        )

        # Write manifest
        manifest_key = write_manifest(run_id, channel_id, results)
        log.info("Manifest written: s3://%s/%s", S3_OUTPUTS_BUCKET, manifest_key)

        succeeded = sum(1 for r in results if r.get("status") == "success")
        failed = sum(1 for r in results if r.get("status") == "failed")

        elapsed = time.time() - step_start
        notify_step_complete("shorts", run_id, [
            {"name": "Tiers", "value": ", ".join(valid_tiers), "inline": True},
            {"name": "Succeeded", "value": str(succeeded), "inline": True},
            {"name": "Failed", "value": str(failed), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0x9B59B6)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": dry_run,
            "shorts_enabled": True,
            "manifest_s3_key": manifest_key,
            "shorts_count": succeeded,
            "shorts_failed": failed,
            "shorts_results": results,
        }

    except Exception as exc:
        log.error("Shorts step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "shorts", exc)
        raise


if __name__ == "__main__":
    # Support both direct invocation and env-var-driven
    event = {}
    if len(sys.argv) > 1:
        event = json.loads(sys.argv[1])
    result = lambda_handler(event)
    print(json.dumps(result, default=str))
    sys.exit(0)

