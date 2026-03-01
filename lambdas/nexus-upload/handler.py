import json
import os
import tempfile
import urllib.parse
import urllib.request
import boto3

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_THUMBNAIL_URL = "https://www.googleapis.com/youtube/v3/thumbnails/set"


def _refresh_access_token(credentials: dict) -> str:
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
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "NexusCloud/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token_data = json.loads(resp.read())
    return token_data["access_token"]


def _upload_video(
    video_path: str,
    metadata: dict,
    access_token: str,
) -> dict:
    file_size = os.path.getsize(video_path)
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
        "User-Agent": "NexusCloud/1.0",
    }
    body = json.dumps(metadata).encode("utf-8")
    init_url = f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status"
    req = urllib.request.Request(
        init_url, data=body, headers=init_headers, method="POST"
    )
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
                "User-Agent": "NexusCloud/1.0",
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

    return response_body or {}


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
            "User-Agent": "NexusCloud/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60):
        pass


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


def _notify_discord(step: str, color: int, run_id: str, fields: list[dict], dry_run: bool = False) -> None:
    """Send a step-level Discord notification. Silently swallows errors."""
    if dry_run:
        return
    try:
        webhook_url = get_secret("nexus/discord_webhook_url").get("url", "")
        if not webhook_url:
            return
        embed = {
            "embeds": [{
                "title": f"🚀 Nexus Cloud — {step}",
                "color": color,
                "fields": [{"name": "Run ID", "value": run_id, "inline": False}] + fields,
                "footer": {"text": "Nexus Cloud Pipeline"},
            }]
        }
        data = json.dumps(embed).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "NexusCloud/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    final_video_s3_key: str = event["final_video_s3_key"]
    primary_thumbnail_s3_key: str = event["primary_thumbnail_s3_key"]
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    thumbnail_s3_keys: list = event.get("thumbnail_s3_keys", [primary_thumbnail_s3_key])
    video_duration_sec: float = float(event.get("video_duration_sec", 0))

    try:
        s3 = boto3.client("s3")

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

        auto_publish = os.environ.get("YOUTUBE_AUTO_PUBLISH", "false").lower() == "true"

        if not auto_publish:
            # ── Manual approval mode ──
            # Save upload-ready metadata to S3; user runs approve_upload.py later.
            pending = {
                "run_id": run_id,
                "profile": profile_name,
                "title": title,
                "description": description,
                "tags": tags,
                "final_video_s3_key": final_video_s3_key,
                "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
                "thumbnail_s3_keys": thumbnail_s3_keys,
                "video_duration_sec": video_duration_sec,
                "status": "pending_approval",
            }
            s3.put_object(
                Bucket=S3_OUTPUTS_BUCKET,
                Key=f"{run_id}/pending_upload.json",
                Body=json.dumps(pending, indent=2).encode("utf-8"),
                ContentType="application/json",
            )

            _notify_discord("Upload Pending Approval", 0xF39C12, run_id, [
                {"name": "Title", "value": title[:100], "inline": False},
                {"name": "Status", "value": "pending approval", "inline": True},
                {"name": "Profile", "value": profile_name, "inline": True},
            ], dry_run=dry_run)

            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": False,
                "video_id": "PENDING_MANUAL_APPROVAL",
                "video_url": "pending://manual-approval-required",
                "title": title,
                "thumbnail_s3_keys": thumbnail_s3_keys,
                "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
                "video_duration_sec": video_duration_sec,
                "auto_publish": False,
            }

        yt_credentials = get_secret("nexus/youtube_credentials")
        access_token = _refresh_access_token(yt_credentials)

        video_metadata = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:500],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": False,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            video_local = os.path.join(tmpdir, "final_video.mp4")
            s3.download_file(S3_OUTPUTS_BUCKET, final_video_s3_key, video_local)

            thumbnail_local = os.path.join(tmpdir, "thumbnail.jpg")
            s3.download_file(S3_OUTPUTS_BUCKET, primary_thumbnail_s3_key, thumbnail_local)

            upload_result = _upload_video(video_local, video_metadata, access_token)
            video_id = upload_result.get("id", "")
            if not video_id:
                raise RuntimeError("YouTube upload returned no video ID")

            _upload_thumbnail(video_id, thumbnail_local, access_token)

        video_url = f"https://www.youtube.com/watch?v={video_id}"

        _notify_discord("Video Uploaded", 0x2ECC71, run_id, [
            {"name": "Title", "value": title[:100], "inline": False},
            {"name": "YouTube URL", "value": video_url, "inline": False},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], dry_run=dry_run)

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
