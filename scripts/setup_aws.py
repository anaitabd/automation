#!/usr/bin/env python3
"""
setup_aws.py — Bootstrap all required AWS resources from .env values.

Creates:
  • 3 S3 buckets  (nexus-assets, nexus-outputs, nexus-config)
  • IAM role for MediaConvert with correct trust policy + S3 permissions
  • Secrets Manager secrets for every service (no Storyblocks)
  • Uploads channel profiles to nexus-config bucket

Writes back AWS_ACCOUNT_ID and MEDIACONVERT_ROLE_ARN into .env automatically.
"""

import json
import os
import sys
import time
import pathlib

import boto3
from botocore.exceptions import ClientError

ENV_PATH = pathlib.Path("/app/.env")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# ────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────

def _print(msg: str) -> None:
    print(f"[setup_aws] {msg}", flush=True)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _update_env_file(updates: dict[str, str]) -> None:
    """Patch key=value lines inside the .env file on disk."""
    if not ENV_PATH.exists():
        return
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    new_lines: list[str] = []
    applied = set()
    for line in lines:
        stripped = line.strip()
        for key, value in updates.items():
            if stripped.startswith(f"{key}="):
                line = f"{key}={value}\n"
                applied.add(key)
                break
        new_lines.append(line)
    # append keys that were not in the file
    for key, value in updates.items():
        if key not in applied:
            new_lines.append(f"{key}={value}\n")
    ENV_PATH.write_text("".join(new_lines))


# ────────────────────────────────────────────────────────────────
# 1. Resolve account ID
# ────────────────────────────────────────────────────────────────

_account_id_cache: str = ""


def get_account_id() -> str:
    global _account_id_cache
    sts = boto3.client("sts", region_name=REGION)
    identity = sts.get_caller_identity()
    _account_id_cache = identity["Account"]
    _print(f"AWS Account ID: {_account_id_cache}")
    return _account_id_cache


def get_account_id_cached() -> str:
    if _account_id_cache:
        return _account_id_cache
    return get_account_id()


# ────────────────────────────────────────────────────────────────
# 2. S3 buckets
# ────────────────────────────────────────────────────────────────

def ensure_bucket(name: str) -> str:
    """Ensure the bucket exists. Returns the actual bucket name used."""
    s3 = boto3.client("s3", region_name=REGION)
    account_id = get_account_id_cached()

    try:
        s3.head_bucket(Bucket=name)
        _print(f"S3 bucket already exists (owned by us): {name}")
        return name
    except ClientError as e:
        code = int(e.response["Error"]["Code"])
        if code == 404:
            # Bucket doesn't exist anywhere — create it below
            pass
        elif code == 403:
            # Bucket exists but owned by someone else — use account-suffixed name
            _print(f"S3 bucket '{name}' owned by another account, trying with account suffix")
            name = f"{name}-{account_id}"
            try:
                s3.head_bucket(Bucket=name)
                _print(f"S3 bucket already exists (owned by us): {name}")
                return name
            except ClientError:
                pass
        else:
            raise

    kwargs: dict = {"Bucket": name}
    if REGION != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": REGION
        }
    try:
        s3.create_bucket(**kwargs)
        _print(f"Created S3 bucket: {name}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        _print(f"S3 bucket already owned by us: {name}")
    except ClientError as e:
        if "BucketAlreadyExists" in str(e):
            # Globally taken — try with account-id suffix
            suffixed = f"{name}-{account_id}" if account_id not in name else f"{name}-v2"
            _print(f"Bucket '{name}' globally taken, trying: {suffixed}")
            try:
                s3.head_bucket(Bucket=suffixed)
                _print(f"S3 bucket already exists: {suffixed}")
                return suffixed
            except ClientError:
                pass
            kwargs["Bucket"] = suffixed
            s3.create_bucket(**kwargs)
            _print(f"Created S3 bucket (with suffix): {suffixed}")
            return suffixed
        else:
            raise
    return name


def upload_profiles(config_bucket: str) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    profiles_dir = pathlib.Path("/app/profiles")
    for profile_file in profiles_dir.glob("*.json"):
        s3.upload_file(
            str(profile_file),
            config_bucket,
            profile_file.name,
            ExtraArgs={"ContentType": "application/json"},
        )
        _print(f"Uploaded profile: {profile_file.name} → s3://{config_bucket}/{profile_file.name}")


# ────────────────────────────────────────────────────────────────
# 3. MediaConvert IAM role
# ────────────────────────────────────────────────────────────────

MEDIACONVERT_ROLE_NAME = "nexus-mediaconvert-role"

MEDIACONVERT_TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "mediaconvert.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
})

MEDIACONVERT_S3_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:ListBucket",
            ],
            "Resource": [
                "arn:aws:s3:::nexus-assets",
                "arn:aws:s3:::nexus-assets/*",
                "arn:aws:s3:::nexus-outputs",
                "arn:aws:s3:::nexus-outputs/*",
            ],
        }
    ],
}


def _update_mediaconvert_policy(assets_bucket: str, outputs_bucket: str) -> None:
    """Update the MediaConvert role policy with actual bucket names."""
    iam = boto3.client("iam", region_name=REGION)
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:ListBucket",
                ],
                "Resource": [
                    f"arn:aws:s3:::{assets_bucket}",
                    f"arn:aws:s3:::{assets_bucket}/*",
                    f"arn:aws:s3:::{outputs_bucket}",
                    f"arn:aws:s3:::{outputs_bucket}/*",
                ],
            }
        ],
    }
    try:
        iam.put_role_policy(
            RoleName=MEDIACONVERT_ROLE_NAME,
            PolicyName="nexus-mediaconvert-s3-access",
            PolicyDocument=json.dumps(policy),
        )
        _print(f"Updated MediaConvert role policy with buckets: {assets_bucket}, {outputs_bucket}")
    except Exception as e:
        _print(f"Warning: could not update MediaConvert policy: {e}")


def ensure_mediaconvert_role(account_id: str, assets_bucket: str = "nexus-assets", outputs_bucket: str = "nexus-outputs") -> str:
    iam = boto3.client("iam", region_name=REGION)
    try:
        resp = iam.get_role(RoleName=MEDIACONVERT_ROLE_NAME)
        role_arn = resp["Role"]["Arn"]
        _print(f"MediaConvert IAM role already exists: {role_arn}")
        return role_arn
    except iam.exceptions.NoSuchEntityException:
        pass

    resp = iam.create_role(
        RoleName=MEDIACONVERT_ROLE_NAME,
        AssumeRolePolicyDocument=MEDIACONVERT_TRUST_POLICY,
        Description="Allows AWS MediaConvert to access Nexus S3 buckets",
    )
    role_arn = resp["Role"]["Arn"]
    _print(f"Created IAM role: {role_arn}")

    # Use dynamic bucket names from the start
    initial_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{assets_bucket}",
                    f"arn:aws:s3:::{assets_bucket}/*",
                    f"arn:aws:s3:::{outputs_bucket}",
                    f"arn:aws:s3:::{outputs_bucket}/*",
                ],
            }
        ],
    }
    iam.put_role_policy(
        RoleName=MEDIACONVERT_ROLE_NAME,
        PolicyName="nexus-mediaconvert-s3-access",
        PolicyDocument=json.dumps(initial_policy),
    )
    _print("Attached initial S3 access policy to MediaConvert role")

    # IAM roles can take a few seconds to propagate
    time.sleep(5)
    return role_arn


# ────────────────────────────────────────────────────────────────
# 4. Secrets Manager
# ────────────────────────────────────────────────────────────────

def _upsert_secret(name: str, secret_dict: dict) -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    secret_string = json.dumps(secret_dict)
    try:
        sm.describe_secret(SecretId=name)
        sm.put_secret_value(SecretId=name, SecretString=secret_string)
        _print(f"Updated secret: {name}")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=name, SecretString=secret_string)
        _print(f"Created secret: {name}")


def create_secrets() -> None:
    _upsert_secret("nexus/perplexity_api_key", {
        "api_key": _env("PERPLEXITY_API_KEY"),
    })
    _upsert_secret("nexus/elevenlabs_api_key", {
        "api_key": _env("ELEVENLABS_API_KEY"),
    })
    _upsert_secret("nexus/pexels_api_key", {
        "api_key": _env("PEXELS_API_KEY"),
        "pixabay_key": _env("PIXABAY_API_KEY"),
    })
    _upsert_secret("nexus/freesound_api_key", {
        "api_key": _env("FREESOUND_API_KEY"),
    })
    _upsert_secret("nexus/youtube_credentials", {
        "client_id": _env("YOUTUBE_CLIENT_ID"),
        "client_secret": _env("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": _env("YOUTUBE_REFRESH_TOKEN"),
    })
    _upsert_secret("nexus/discord_webhook_url", {
        "url": _env("DISCORD_WEBHOOK_URL"),
    })
    _upsert_secret("nexus/db_credentials", {
        "host": _env("DB_HOST", "postgres"),
        "port": _env("DB_PORT", "5432"),
        "dbname": _env("DB_NAME") or "nexus",
        "user": _env("DB_USER", "nexus_user"),
        "password": _env("DB_PASSWORD"),
    })


# ────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────

def main() -> None:
    _print("Starting AWS resource bootstrap …")

    # Validate credentials are set
    if not _env("AWS_ACCESS_KEY_ID") or not _env("AWS_SECRET_ACCESS_KEY"):
        _print("ERROR: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in .env")
        sys.exit(1)

    account_id = get_account_id()

    # S3 buckets — names may get account-id suffix if globally taken
    actual_assets = ensure_bucket(_env("ASSETS_BUCKET", "nexus-assets"))
    actual_outputs = ensure_bucket(_env("OUTPUTS_BUCKET", "nexus-outputs"))
    actual_config = ensure_bucket(_env("CONFIG_BUCKET", "nexus-config"))

    # Upload profile configs
    upload_profiles(actual_config)

    # MediaConvert role
    mc_role_arn = ensure_mediaconvert_role(account_id, actual_assets, actual_outputs)

    # Update MediaConvert S3 policy with actual bucket names
    _update_mediaconvert_policy(actual_assets, actual_outputs)

    # Secrets Manager
    create_secrets()

    # Write back auto-populated values to .env
    _update_env_file({
        "AWS_ACCOUNT_ID": account_id,
        "MEDIACONVERT_ROLE_ARN": mc_role_arn,
        "ASSETS_BUCKET": actual_assets,
        "OUTPUTS_BUCKET": actual_outputs,
        "CONFIG_BUCKET": actual_config,
    })
    _print(f"Wrote AWS_ACCOUNT_ID={account_id}, MEDIACONVERT_ROLE_ARN={mc_role_arn} to .env")
    _print(f"Buckets: assets={actual_assets}, outputs={actual_outputs}, config={actual_config}")

    _print("✅ AWS bootstrap complete!")


if __name__ == "__main__":
    main()


