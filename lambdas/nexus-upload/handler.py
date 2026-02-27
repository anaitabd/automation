"""
nexus-upload Lambda
Runtime: Python 3.12 | Memory: 512 MB | Timeout: 10 min

Downloads the final video and primary thumbnail from S3 and uploads
them to YouTube via the YouTube Data API v3 (OAuth2).
OAuth credentials are stored in AWS Secrets Manager.
"""

import json
import os
import tempfile
import urllib.parse
import urllib.request
import boto3

# ---------------------------------------------------------------------------
# Secrets cache
# ---------------------------------------------------------------------------
_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
S3_OUTPUTS_BUCKET = "nexus-outputs"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_THUMBNAIL_URL = "https://www.googleapis.com/youtube/v3/thumbnails/set"


# ---------------------------------------------------------------------------
# OAuth2 token refresh
# ---------------------------------------------------------------------------
def _refresh_access_token(credentials: dict) -> str:
    """Return a fresh access token using the refresh token."""
    data = urllib.parse.urlencode(
        {
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "refresh_token": credentials["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        YOUTUBE_TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token_data = json.loads(resp.read())
    return token_data["access_token"]


# ---------------------------------------------------------------------------
# YouTube resumable upload
# ---------------------------------------------------------------------------
def _upload_video(
    video_path: str,
    metadata: dict,
    access_token: str,
) -> dict:
    """Upload video using the YouTube resumable upload protocol."""
    # Step 1: Initiate resumable upload session
    file_size = os.path.getsize(video_path)
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
    }
    body = json.dumps(metadata).encode("utf-8")
    init_url = f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status"
    req = urllib.request.Request(
        init_url, data=body, headers=init_headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        session_uri = resp.headers["Location"]

    # Step 2: Upload the file
    chunk_size = 8 * 1024 * 1024  # 8 MB chunks
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
                if e.code == 308:  # Resume Incomplete — continue
                    uploaded += len(chunk)
                    continue
                raise
            uploaded += len(chunk)

    return response_body or {}


# ---------------------------------------------------------------------------
# Thumbnail upload
# ---------------------------------------------------------------------------
def _upload_thumbnail(video_id: str, thumbnail_path: str, access_token: str) -> None:
    file_size = os.path.getsize(thumbnail_path)
    with open(thumbnail_path, "rb") as f:
        data = f.read()
    url = f"{YOUTUBE_THUMBNAIL_URL}?videoId={video_id}&uploadType=media"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "image/jpeg",
            "Content-Length": str(file_size),
        },
    )
    with urllib.request.urlopen(req, timeout=60):
        pass


# ---------------------------------------------------------------------------
# Error writer
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    final_video_s3_key: str = event["final_video_s3_key"]
    primary_thumbnail_s3_key: str = event["primary_thumbnail_s3_key"]
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    # Echo through for downstream states
    thumbnail_s3_keys: list = event.get("thumbnail_s3_keys", [primary_thumbnail_s3_key])
    video_duration_sec: float = float(event.get("video_duration_sec", 0))

    try:
        s3 = boto3.client("s3")

        # Load script for metadata
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        title = script.get("title", "Untitled")
        description = script.get("description", "")
        tags = script.get("tags", [])
        cta = script.get("cta", "")
        if cta:
            description = f"{description}\n\n{cta}"

        if dry_run:
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "title": title,
                "video_id": "DRY_RUN_VIDEO_ID",
                "video_url": "https://youtube.com/watch?v=DRY_RUN_VIDEO_ID",
                "thumbnail_s3_keys": thumbnail_s3_keys,
                "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
                "video_duration_sec": video_duration_sec,
            }

        # Load OAuth credentials
        yt_credentials = get_secret("nexus/youtube_credentials")
        access_token = _refresh_access_token(yt_credentials)

        video_metadata = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:500],
                "categoryId": "22",  # People & Blogs — override in profile if needed
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            # Download video
            video_local = os.path.join(tmpdir, "final_video.mp4")
            s3.download_file(S3_OUTPUTS_BUCKET, final_video_s3_key, video_local)

            # Download thumbnail
            thumbnail_local = os.path.join(tmpdir, "thumbnail.jpg")
            s3.download_file(S3_OUTPUTS_BUCKET, primary_thumbnail_s3_key, thumbnail_local)

            # Upload video
            upload_result = _upload_video(video_local, video_metadata, access_token)
            video_id = upload_result.get("id", "")
            if not video_id:
                raise RuntimeError("YouTube upload returned no video ID")

            # Upload thumbnail
            _upload_thumbnail(video_id, thumbnail_local, access_token)

        video_url = f"https://www.youtube.com/watch?v={video_id}"

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "video_id": video_id,
            "video_url": video_url,
            "title": title,
            "thumbnail_s3_keys": thumbnail_s3_keys,
            "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
            "video_duration_sec": video_duration_sec,
        }

    except Exception as exc:
        _write_error(run_id, "upload", exc)
        raise
