"""nexus-channel-setup — Orchestrates brand design → logo gen → intro/outro.

Invoked asynchronously by nexus-api after POST /channel/create.
Calls three sub-Lambdas synchronously in sequence, then updates the
channel DB row with brand kit data and status='active'.
"""

import json
import logging
import os
import sys
import time

import boto3

# nexus_pipeline_utils is copied into this directory at deploy time
from nexus_pipeline_utils import notify_step_start, notify_step_complete, get_logger

log = get_logger("nexus-channel-setup")

_cache: dict = {}

BRAND_DESIGNER_FUNCTION = os.environ.get("BRAND_DESIGNER_FUNCTION", "nexus-brand-designer")
LOGO_GEN_FUNCTION = os.environ.get("LOGO_GEN_FUNCTION", "nexus-logo-gen")
INTRO_OUTRO_FUNCTION = os.environ.get("INTRO_OUTRO_FUNCTION", "nexus-intro-outro")
OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")


def _get_db_credentials() -> dict:
    if "db_creds" not in _cache:
        sm = boto3.client("secretsmanager")
        secret = json.loads(
            sm.get_secret_value(SecretId="nexus/db_credentials")["SecretString"]
        )
        _cache["db_creds"] = secret
    return _cache["db_creds"]


def _get_db_connection():
    import psycopg2
    creds = _get_db_credentials()
    dbname = creds.get("dbname") or "nexus"
    return psycopg2.connect(
        host=creds["host"],
        port=creds.get("port", 5432),
        dbname=dbname,
        user=creds["user"],
        password=creds["password"],
        connect_timeout=10,
    )


def _update_channel(channel_id: str, brand: dict, voice_id: str, status: str) -> None:
    """Update channel row with brand kit and status."""
    conn = _get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE nexus_channels
                    SET brand = %s, voice_id = %s, status = %s,
                        stats = jsonb_set(COALESCE(stats, '{}'), '{status}', %s::jsonb),
                        updated_at = NOW()
                    WHERE channel_id = %s
                    """,
                    (json.dumps(brand), voice_id, status, json.dumps(status), channel_id),
                )
    finally:
        conn.close()


def _invoke_lambda(function_name: str, payload: dict) -> dict:
    """Invoke a Lambda synchronously and return parsed response."""
    client = boto3.client("lambda")
    resp = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    response_payload = json.loads(resp["Payload"].read())

    if resp.get("FunctionError"):
        error_msg = response_payload.get("errorMessage", str(response_payload))
        raise RuntimeError(f"{function_name} failed: {error_msg}")

    return response_payload


def _write_error(channel_id: str, step: str, exc: Exception) -> None:
    """Write error details to S3 for debugging."""
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=OUTPUTS_BUCKET,
            Key=f"channels/{channel_id}/errors/{step}.json",
            Body=json.dumps({
                "step": step,
                "error": str(exc),
                "type": type(exc).__name__,
            }),
            ContentType="application/json",
        )
    except Exception:
        log.warning("Failed to write error to S3 for %s/%s", channel_id, step)


def lambda_handler(event: dict, context) -> dict:
    channel_id = event["channel_id"]
    channel_name = event["channel_name"]
    niche = event["niche"]
    profile = event.get("profile", "documentary")
    style_hints = event.get("style_hints", "")

    log.info("Channel setup starting: %s (%s) — profile=%s", channel_name, channel_id, profile)
    start_time = time.time()

    brand = {}
    voice_id = ""

    try:
        # ── Step 1: Brand Design (Claude) ─────────────────────────
        log.info("Step 1/3: Brand Design")
        brand_result = _invoke_lambda(BRAND_DESIGNER_FUNCTION, {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "niche": niche,
            "profile": profile,
            "style_hints": style_hints,
        })
        brand = brand_result.get("brand", {})
        voice_id = brand_result.get("voice_id", "")
        log.info("Brand design complete: %s", json.dumps(brand, indent=2)[:500])

        # ── Step 2: Logo Generation (Nova Canvas) ─────────────────
        log.info("Step 2/3: Logo Generation")
        logo_result = _invoke_lambda(LOGO_GEN_FUNCTION, {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "niche": niche,
            "profile": profile,
            "brand": brand,
        })
        brand["logo_s3"] = logo_result.get("logo_s3_key", "")
        log.info("Logo generated: %s", brand["logo_s3"])

        # ── Step 3: Intro / Outro (Nova Reel + FFmpeg) ────────────
        log.info("Step 3/3: Intro / Outro")
        intro_outro_result = _invoke_lambda(INTRO_OUTRO_FUNCTION, {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "niche": niche,
            "profile": profile,
            "brand": brand,
        })
        brand["intro_s3"] = intro_outro_result.get("intro_s3_key", "")
        brand["outro_s3"] = intro_outro_result.get("outro_s3_key", "")
        log.info("Intro/outro generated: intro=%s outro=%s", brand["intro_s3"], brand["outro_s3"])

        # ── Update channel to active ──────────────────────────────
        _update_channel(channel_id, brand, voice_id, "active")

        elapsed = time.time() - start_time
        log.info("Channel setup complete for %s in %.1fs", channel_name, elapsed)

        return {
            "channel_id": channel_id,
            "status": "active",
            "brand": brand,
            "voice_id": voice_id,
            "elapsed_sec": round(elapsed, 1),
        }

    except Exception as exc:
        log.error("Channel setup failed for %s: %s", channel_id, exc, exc_info=True)
        _write_error(channel_id, "channel-setup", exc)
        try:
            _update_channel(channel_id, brand, voice_id, "error")
        except Exception:
            log.warning("Failed to update channel status to error")
        raise

