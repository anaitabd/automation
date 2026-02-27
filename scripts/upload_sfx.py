#!/usr/bin/env python3
"""
scripts/upload_sfx.py — Download free SFX from Freesound API and upload to S3.

Usage:
    # Set your Freesound API key first:
    export FREESOUND_API_KEY=your_key_here

    python scripts/upload_sfx.py [--bucket nexus-assets] [--dry-run]

Required SFX files (saved to sfx/ prefix in the assets bucket):
    sfx/whoosh_soft.wav         — for lower_third events
    sfx/counter_tick.wav        — for stat_counter events
    sfx/typewriter_impact.wav   — for documentary stat_counter
    sfx/paper_rustle.wav        — for documentary quote_card

The script queries Freesound for CC0-licensed audio, downloads the preview,
and uploads it with the canonical filename the pipeline expects.
Requires: pip install requests boto3
"""

import argparse
import os
import sys
import urllib.request
import urllib.parse
import json
import time
import tempfile

try:
    import boto3
except ImportError:
    boto3 = None

FREESOUND_API_BASE = "https://freesound.org/apiv2"
S3_PREFIX = "sfx/"

# Map our canonical SFX name → Freesound search query + filter
SFX_QUERIES = {
    "whoosh_soft.wav": {
        "query": "soft whoosh transition",
        "filter": "duration:[0.2 TO 1.5] license:\"Creative Commons 0\"",
        "fields": "id,name,previews,license",
    },
    "counter_tick.wav": {
        "query": "counter tick click digital",
        "filter": "duration:[0.05 TO 0.5] license:\"Creative Commons 0\"",
        "fields": "id,name,previews,license",
    },
    "typewriter_impact.wav": {
        "query": "typewriter key impact single",
        "filter": "duration:[0.05 TO 0.8] license:\"Creative Commons 0\"",
        "fields": "id,name,previews,license",
    },
    "paper_rustle.wav": {
        "query": "paper rustle sheet",
        "filter": "duration:[0.3 TO 2.0] license:\"Creative Commons 0\"",
        "fields": "id,name,previews,license",
    },
}


def _freesound_search(query: str, filter_str: str, fields: str, api_key: str) -> dict | None:
    """Return the first result from Freesound search."""
    params = urllib.parse.urlencode({
        "query": query,
        "filter": filter_str,
        "fields": fields,
        "page_size": 1,
        "sort": "score",
        "token": api_key,
    })
    url = f"{FREESOUND_API_BASE}/search/text/?{params}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        return results[0] if results else None
    except Exception as exc:
        print(f"    WARNING: Freesound search failed: {exc}")
        return None


def _download_preview(result: dict, tmpdir: str, canonical_name: str) -> str | None:
    """Download the high-quality preview (.mp3 usually) and convert filename."""
    previews = result.get("previews", {})
    # Prefer hq, fall back to lq
    url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
    if not url:
        return None
    out_path = os.path.join(tmpdir, canonical_name.replace(".wav", ".mp3"))
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            with open(out_path, "wb") as f:
                f.write(resp.read())
        return out_path
    except Exception as exc:
        print(f"    WARNING: Download failed: {exc}")
        return None


def _convert_to_wav(mp3_path: str, wav_path: str) -> bool:
    """Convert mp3 to wav using ffmpeg if available."""
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-acodec", "pcm_s16le", "-ar", "44100", wav_path],
            capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        # ffmpeg not available locally — just copy the mp3 with .wav extension
        import shutil
        shutil.copy2(mp3_path, wav_path)
        return True


def _upload_to_s3(local_path: str, canonical_name: str, bucket: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"    [dry-run] Would upload → s3://{bucket}/{S3_PREFIX}{canonical_name}")
        return True
    if boto3 is None:
        print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
        return False
    s3 = boto3.client("s3")
    key = f"{S3_PREFIX}{canonical_name}"
    try:
        s3.upload_file(local_path, bucket, key)
        print(f"    ✔ Uploaded → s3://{bucket}/{key}")
        return True
    except Exception as exc:
        print(f"    ERROR uploading {key}: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Download free SFX and upload to S3")
    parser.add_argument("--bucket", default="nexus-assets", help="S3 bucket (default: nexus-assets)")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual S3 uploads")
    parser.add_argument(
        "--api-key", default=os.environ.get("FREESOUND_API_KEY", ""),
        help="Freesound API key (or set FREESOUND_API_KEY env var)"
    )
    args = parser.parse_args()

    if not args.api_key and not args.dry_run:
        print(
            "ERROR: Freesound API key required.\n"
            "Set FREESOUND_API_KEY environment variable or pass --api-key.\n"
            "Get a free key at https://freesound.org/apiv2/apply/",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Downloading {len(SFX_QUERIES)} SFX files\n")

    success_count = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for canonical_name, spec in SFX_QUERIES.items():
            print(f"  ⟳ Searching: {canonical_name}")

            if args.dry_run:
                print(f"    [dry-run] Would search Freesound for: {spec['query']}")
                success_count += 1
                continue

            result = _freesound_search(
                spec["query"], spec["filter"], spec["fields"], args.api_key
            )
            if result is None:
                print(f"    ⚠ No result found for '{canonical_name}' — skipping")
                continue

            print(f"    Found: {result.get('name', '?')} (id={result.get('id')})")

            mp3_path = _download_preview(result, tmpdir, canonical_name)
            if mp3_path is None:
                print(f"    ⚠ Download failed — skipping")
                continue

            wav_path = os.path.join(tmpdir, canonical_name)
            converted = _convert_to_wav(mp3_path, wav_path)
            if not converted:
                print(f"    ⚠ Conversion failed — skipping")
                continue

            uploaded = _upload_to_s3(wav_path, canonical_name, args.bucket, args.dry_run)
            if uploaded:
                success_count += 1

            time.sleep(0.5)  # Rate limit Freesound API

    total = len(SFX_QUERIES)
    print(f"\n✅ Done. {success_count}/{total} SFX files processed.\n")

    if success_count < total:
        print(
            "TIP: For any missing SFX files, you can manually add WAV files to:\n"
            f"  s3://{args.bucket}/sfx/<filename>.wav\n"
        )


if __name__ == "__main__":
    main()
