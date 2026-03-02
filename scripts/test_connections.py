#!/usr/bin/env python3
"""
test_connections.py — Verify connectivity to every service used by Nexus Cloud.

Usage (standalone):
    python scripts/test_connections.py

Usage (Docker):
    docker compose --profile test run test-connections

Checks:
  ✓ AWS credentials (STS)
  ✓ S3 buckets (list objects)
  ✓ Secrets Manager (get each secret)
  ✓ AWS Bedrock — us.anthropic.claude-3-sonnet-20240229-v1:0 (invoke)
  ✓ Perplexity API (reachability)
  ✓ ElevenLabs API (reachability)
  ✓ Pexels API (reachability)
  ✓ Discord Webhook (send test embed)
  ✓ PostgreSQL (connect + SELECT 1)
  ✓ MediaConvert (describe endpoints)
"""

import json
import os
import sys
import urllib.request
import urllib.error

import boto3
import pytest

REGION = os.environ.get("AWS_REGION", "us-east-1")
RESULTS: list[tuple[str, bool, str]] = []


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def _aws_tests_enabled() -> bool:
    return os.environ.get("RUN_AWS_TESTS", "").strip() == "1"


def _require_aws_tests() -> None:
    if not _aws_tests_enabled():
        pytest.skip("AWS integration tests skipped (set RUN_AWS_TESTS=1 to enable)")


# ─────────────────────────────────────────────────
# 1. AWS STS (credentials check)
# ─────────────────────────────────────────────────
def test_aws_sts() -> None:
    _require_aws_tests()
    try:
        sts = boto3.client("sts", region_name=REGION)
        identity = sts.get_caller_identity()
        _record("AWS STS (credentials)", True, f"Account={identity['Account']}")
    except Exception as e:
        _record("AWS STS (credentials)", False, str(e))


# ─────────────────────────────────────────────────
# 2. S3 buckets
# ─────────────────────────────────────────────────
def test_s3_buckets() -> None:
    _require_aws_tests()
    s3 = boto3.client("s3", region_name=REGION)
    buckets = [
        _env("ASSETS_BUCKET", "nexus-assets"),
        _env("OUTPUTS_BUCKET", "nexus-outputs"),
        _env("CONFIG_BUCKET", "nexus-config"),
    ]
    for bucket in buckets:
        try:
            s3.head_bucket(Bucket=bucket)
            # Quick list to verify read access
            s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            _record(f"S3 bucket: {bucket}", True)
        except Exception as e:
            _record(f"S3 bucket: {bucket}", False, str(e))


# ─────────────────────────────────────────────────
# 3. Secrets Manager
# ─────────────────────────────────────────────────
def test_secrets_manager() -> None:
    _require_aws_tests()
    sm = boto3.client("secretsmanager", region_name=REGION)
    secrets = [
        "nexus/perplexity_api_key",
        "nexus/elevenlabs_api_key",
        "nexus/pexels_api_key",
        "nexus/youtube_credentials",
        "nexus/discord_webhook_url",
        "nexus/db_credentials",
    ]
    for name in secrets:
        try:
            resp = sm.get_secret_value(SecretId=name)
            data = json.loads(resp["SecretString"])
            keys = list(data.keys())
            _record(f"Secret: {name}", True, f"keys={keys}")
        except Exception as e:
            _record(f"Secret: {name}", False, str(e))


# ─────────────────────────────────────────────────
# 4. AWS Bedrock — invoke claude-3-sonnet
# ─────────────────────────────────────────────────
def test_bedrock() -> None:
    _require_aws_tests()
    try:
        client = boto3.client("bedrock-runtime", region_name=REGION)
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hello in one word."}],
        })
        resp = client.invoke_model(
            modelId="us.anthropic.claude-3-sonnet-20240229-v1:0",
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(resp["body"].read())
        text = result["content"][0]["text"][:60]
        _record("Bedrock (claude-3-sonnet)", True, f"response={text!r}")
    except Exception as e:
        _record("Bedrock (claude-3-sonnet)", False, str(e))


# ─────────────────────────────────────────────────
# 5. Perplexity API
# ─────────────────────────────────────────────────
def test_perplexity() -> None:
    api_key = _env("PERPLEXITY_API_KEY")
    if not api_key:
        _record("Perplexity API", False, "PERPLEXITY_API_KEY not set")
        return
    try:
        body = json.dumps({
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
        }).encode()
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "NexusCloud/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"][:60]
        _record("Perplexity API (sonar-pro)", True, f"response={text!r}")
    except Exception as e:
        _record("Perplexity API (sonar-pro)", False, str(e))


# ─────────────────────────────────────────────────
# 6. ElevenLabs API
# ─────────────────────────────────────────────────
def test_elevenlabs() -> None:
    api_key = _env("ELEVENLABS_API_KEY")
    if not api_key:
        _record("ElevenLabs API", False, "ELEVENLABS_API_KEY not set")
        return
    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user",
            headers={
                "xi-api-key": api_key,
                "User-Agent": "NexusCloud/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            sub = data.get("subscription", {}).get("tier", "unknown")
        _record("ElevenLabs API", True, f"tier={sub}")
    except Exception as e:
        _record("ElevenLabs API", False, str(e))


# ─────────────────────────────────────────────────
# 7. Pexels API
# ─────────────────────────────────────────────────
def test_pexels() -> None:
    api_key = _env("PEXELS_API_KEY").strip()
    if not api_key:
        _record("Pexels API", False, "PEXELS_API_KEY not set")
        return
    try:
        req = urllib.request.Request(
            "https://api.pexels.com/v1/search?query=nature&per_page=1",
            headers={
                "Authorization": api_key,
                "User-Agent": "NexusCloud/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            total = data.get("total_results", 0)
        _record("Pexels API", True, f"total_results={total}")
    except Exception as e:
        _record("Pexels API", False, str(e))


# ─────────────────────────────────────────────────
# 8. Discord Webhook
# ─────────────────────────────────────────────────
def test_discord() -> None:
    url = _env("DISCORD_WEBHOOK_URL").strip()
    if not url:
        _record("Discord Webhook", False, "DISCORD_WEBHOOK_URL not set")
        return
    try:
        embed = {
            "embeds": [
                {
                    "title": "🔧 Nexus Cloud — Connection Test",
                    "description": "This is an automated connectivity check.",
                    "color": 0x5865F2,
                    "footer": {"text": "test_connections.py"},
                }
            ]
        }
        data = json.dumps(embed).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "NexusCloud/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
        _record("Discord Webhook", status in (200, 204), f"HTTP {status}")
    except Exception as e:
        _record("Discord Webhook", False, str(e))


# ─────────────────────────────────────────────────
# 9. PostgreSQL
# ─────────────────────────────────────────────────
def test_postgres() -> None:
    try:
        import psycopg2
        db_name = _env("DB_NAME") or "nexus"
        if not _env("DB_NAME"):
            print("  ⚠️  DB_NAME is empty/unset, falling back to 'nexus'", flush=True)
        conn = psycopg2.connect(
            host=_env("DB_HOST", "postgres"),
            port=int(_env("DB_PORT", "5432")),
            dbname=db_name,
            user=_env("DB_USER", "nexus_user"),
            password=_env("DB_PASSWORD"),
            connect_timeout=5,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        conn.close()
        _record("PostgreSQL", True, f"{_env('DB_HOST')}:{_env('DB_PORT')}/{_env('DB_NAME')}")
    except Exception as e:
        _record("PostgreSQL", False, str(e))


# ─────────────────────────────────────────────────
# 10. MediaConvert endpoint
# ─────────────────────────────────────────────────
def test_mediaconvert() -> None:
    _require_aws_tests()
    try:
        mc = boto3.client("mediaconvert", region_name=REGION)
        endpoints = mc.describe_endpoints(MaxResults=1)
        url = endpoints["Endpoints"][0]["Url"]
        _record("MediaConvert", True, f"endpoint={url}")
    except Exception as e:
        _record("MediaConvert", False, str(e))


# ─────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────
def main() -> None:
    print("\n" + "=" * 60, flush=True)
    print("  Nexus Cloud — Service Connectivity Test", flush=True)
    print("=" * 60 + "\n", flush=True)

    test_aws_sts()
    test_s3_buckets()
    test_secrets_manager()
    test_bedrock()
    test_perplexity()
    test_elevenlabs()
    test_pexels()
    test_discord()
    test_postgres()
    test_mediaconvert()

    print("\n" + "=" * 60, flush=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    total = len(RESULTS)
    print(f"  Results: {passed}/{total} passed, {failed} failed", flush=True)
    print("=" * 60 + "\n", flush=True)

    if failed > 0:
        print("Failed checks:", flush=True)
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  ❌  {name}: {detail}", flush=True)
        sys.exit(1)
    else:
        print("All services connected successfully! 🎉\n", flush=True)


if __name__ == "__main__":
    main()

