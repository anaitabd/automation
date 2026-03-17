#!/usr/bin/env python3
"""One-time setup script: upload a licensed music library to S3 and generate manifest.json.

USAGE:
    python3 scripts/setup_music_library.py --folder /path/to/music --bucket nexus-assets

The script reads all .mp3/.wav/.ogg files from --folder, infers the mood from the filename
prefix (e.g. dark_tension_01.mp3 → mood=dark_tension), uploads them to
s3://nexus-assets/music/{mood}/ and writes a manifest to s3://nexus-assets/music/manifest.json.

Filename convention:
    {mood}_{index}.mp3      e.g.  dark_tension_01.mp3, upbeat_03.mp3
    {mood}-{index}.mp3      e.g.  cinematic-02.mp3

At runtime, nexus-audio and nexus-shorts/audio_mixer.py check for this manifest and use the
S3 library first, falling back to Pixabay if the manifest does not exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("setup_music_library")

SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".ogg", ".aac", ".m4a")
DEFAULT_BUCKET = "nexus-assets"
MANIFEST_KEY = "music/manifest.json"


def _infer_mood(filename: str) -> str:
    """Infer mood from filename convention.

    Accepts:
        dark_tension_01.mp3  → dark_tension
        upbeat-02.wav        → upbeat
        cinematic_warm_03.mp3 → cinematic_warm
    Falls back to 'unknown' if no recognisable pattern.
    """
    base = os.path.splitext(filename)[0]
    # Strip trailing _NN or -NN numeric suffix
    parts_underscore = base.rsplit("_", 1)
    if len(parts_underscore) == 2 and parts_underscore[1].isdigit():
        return parts_underscore[0]

    parts_dash = base.rsplit("-", 1)
    if len(parts_dash) == 2 and parts_dash[1].isdigit():
        return parts_dash[0]

    return base  # whole stem is the mood


def _collect_tracks(folder: str) -> dict[str, list[str]]:
    """Walk the folder and collect tracks grouped by mood.

    Returns {mood: [filename, ...]} sorted alphabetically per mood.
    """
    manifest: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(folder):
        for fname in sorted(files):
            if not any(fname.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                continue
            mood = _infer_mood(fname)
            manifest.setdefault(mood, []).append(fname)
    return manifest


def upload_library(folder: str, bucket: str, dry_run: bool = False) -> dict:
    """Upload all tracks to S3 and write manifest.json.

    Returns the manifest dict.
    """
    try:
        import boto3
    except ImportError:
        log.error("boto3 is required. Install it with: pip install boto3")
        sys.exit(1)

    s3 = boto3.client("s3")

    manifest = _collect_tracks(folder)
    if not manifest:
        log.warning("No supported audio files found in %s", folder)
        return {}

    total = sum(len(v) for v in manifest.values())
    log.info("Found %d tracks across %d moods: %s", total, len(manifest), list(manifest.keys()))

    uploaded = 0
    for mood, filenames in manifest.items():
        for fname in filenames:
            local_path = None
            # Search for the file in the folder tree
            for root, _dirs, files in os.walk(folder):
                if fname in files:
                    local_path = os.path.join(root, fname)
                    break
            if not local_path:
                log.warning("File not found in folder tree: %s", fname)
                continue

            s3_key = f"music/{mood}/{fname}"
            if dry_run:
                log.info("[DRY RUN] Would upload %s → s3://%s/%s", local_path, bucket, s3_key)
            else:
                log.info("Uploading %s → s3://%s/%s", fname, bucket, s3_key)
                s3.upload_file(
                    local_path, bucket, s3_key,
                    ExtraArgs={"ContentType": _content_type(fname)},
                )
            uploaded += 1

    # Write manifest.json
    manifest_body = json.dumps(manifest, indent=2).encode("utf-8")
    if dry_run:
        log.info("[DRY RUN] Would write manifest to s3://%s/%s", bucket, MANIFEST_KEY)
        log.info("Manifest preview:\n%s", json.dumps(manifest, indent=2))
    else:
        s3.put_object(
            Bucket=bucket,
            Key=MANIFEST_KEY,
            Body=manifest_body,
            ContentType="application/json",
        )
        log.info("Manifest written to s3://%s/%s", bucket, MANIFEST_KEY)

    log.info("Done. %d/%d tracks uploaded.", uploaded, total)
    return manifest


def _content_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }.get(ext, "application/octet-stream")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a music library to S3 and generate manifest.json",
    )
    parser.add_argument(
        "--folder", required=True,
        help="Path to the local folder containing licensed music files",
    )
    parser.add_argument(
        "--bucket", default=DEFAULT_BUCKET,
        help=f"S3 bucket name (default: {DEFAULT_BUCKET})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be uploaded without making any S3 calls",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.folder):
        log.error("Folder does not exist: %s", args.folder)
        sys.exit(1)

    upload_library(args.folder, args.bucket, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
