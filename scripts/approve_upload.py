#!/usr/bin/env python3
"""
approve_upload.py — Manually approve and upload a pending video to YouTube.

Usage:
    python scripts/approve_upload.py <run_id>

Reads the pending_upload.json from S3, downloads the video + thumbnail,
uploads to YouTube as private, and updates the metadata.
"""

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

import boto3
from dotenv import load_dotenv

load_dotenv()

REGION = os.environ.get("AWS_REGION", "us-east-1")
OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_THUMBNAIL_URL = "https://www.googleapis.com/youtube/v3/thumbnails/set"


def _refresh_access_token() -> str:
    data = urllib.parse.urlencode({
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        YOUTUBE_TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token_data = json.loads(resp.read())
    return token_data["access_token"]


def _upload_video(video_path: str, metadata: dict, access_token: str) -> str:
    file_size = os.path.getsize(video_path)
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
    }
    body = json.dumps(metadata).encode()
    init_url = f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status"
    req = urllib.request.Request(init_url, data=body, headers=init_headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        session_uri = resp.headers["Location"]

    chunk_size = 8 * 1024 * 1024
    uploaded = 0
    response_body = None

    with open(video_path, "rb") as f:
        while uploaded < file_size:
            chunk = f.read(chunk_size)
            end_byte = uploaded + len(chunk) - 1
            chunk_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {uploaded}-{end_byte}/{file_size}",
                "Content-Length": str(len(chunk)),
            }
            chunk_req = urllib.request.Request(
                session_uri, data=chunk, headers=chunk_headers, method="PUT"
            )
            try:
                with urllib.request.urlopen(chunk_req, timeout=120) as resp:
                    if resp.status == 200:
                        response_body = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    uploaded += len(chunk)
                    continue
                raise
            uploaded += len(chunk)

    return (response_body or {}).get("id", "")


def _upload_thumbnail(video_id: str, thumbnail_path: str, access_token: str) -> None:
    with open(thumbnail_path, "rb") as f:
        data = f.read()
    url = f"{YOUTUBE_THUMBNAIL_URL}?videoId={video_id}&uploadType=media"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(data)),
        },
    )
    with urllib.request.urlopen(req, timeout=60):
        pass


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/approve_upload.py <run_id>")
        sys.exit(1)

    run_id = sys.argv[1]
    s3 = boto3.client("s3", region_name=REGION)

    # Fetch pending upload metadata
    try:
        obj = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=f"{run_id}/pending_upload.json")
        pending = json.loads(obj["Body"].read())
    except Exception as e:
        print(f"❌ Could not find pending upload for run_id={run_id}: {e}")
        sys.exit(1)

    print(f"\n📋 Pending upload for run: {run_id}")
    print(f"   Title:    {pending['title']}")
    print(f"   Profile:  {pending['profile']}")
    print(f"   Duration: {pending.get('video_duration_sec', 0):.0f}s")
    print(f"   Video:    s3://{OUTPUTS_BUCKET}/{pending['final_video_s3_key']}")
    print()

    confirm = input("Proceed with YouTube upload? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    # Validate YouTube credentials
    for key in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        if not os.environ.get(key):
            print(f"❌ {key} is not set in .env")
            sys.exit(1)

    print("🔑 Refreshing YouTube access token …")
    access_token = _refresh_access_token()

    with tempfile.TemporaryDirectory() as tmpdir:
        video_local = os.path.join(tmpdir, "final_video.mp4")
        thumb_local = os.path.join(tmpdir, "thumbnail.jpg")

        print("⬇️  Downloading video from S3 …")
        s3.download_file(OUTPUTS_BUCKET, pending["final_video_s3_key"], video_local)

        print("⬇️  Downloading thumbnail from S3 …")
        s3.download_file(OUTPUTS_BUCKET, pending["primary_thumbnail_s3_key"], thumb_local)

        metadata = {
            "snippet": {
                "title": pending["title"][:100],
                "description": pending.get("description", "")[:5000],
                "tags": pending.get("tags", [])[:500],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
            },
        }

        print("📤 Uploading video to YouTube (private) …")
        video_id = _upload_video(video_local, metadata, access_token)
        if not video_id:
            print("❌ YouTube upload returned no video ID")
            sys.exit(1)

        print(f"🖼️  Setting thumbnail for video {video_id} …")
        _upload_thumbnail(video_id, thumb_local, access_token)

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print("\n✅ Upload complete!")
    print(f"   Video ID:  {video_id}")
    print(f"   URL:       {video_url}")
    print("   Status:    private (change to public in YouTube Studio)")

    # Update pending metadata in S3
    pending["status"] = "uploaded"
    pending["video_id"] = video_id
    pending["video_url"] = video_url
    s3.put_object(
        Bucket=OUTPUTS_BUCKET,
        Key=f"{run_id}/pending_upload.json",
        Body=json.dumps(pending, indent=2).encode(),
        ContentType="application/json",
    )


if __name__ == "__main__":
    main()

