import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import boto3
from boto3.s3.transfer import TransferConfig
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-upload")

_cache: dict = {}
_S3_MULTIPART_THRESHOLD = 100 * 1024 * 1024
_S3_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=_S3_MULTIPART_THRESHOLD,
    max_concurrency=10,
    multipart_chunksize=16 * 1024 * 1024,
)

BEDROCK_SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


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


def _bedrock_invoke(prompt: str, max_tokens: int = 512) -> str:
    """Invoke Claude Sonnet via Bedrock and return the response text."""
    client = boto3.client("bedrock-runtime")
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
    response = client.invoke_model(
        modelId=BEDROCK_SONNET,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    return result["content"][0]["text"].strip()


def _generate_seo_title(script_text: str, profile_name: str, niche: str) -> str:
    """Generate a YouTube SEO title via Claude Sonnet.

    For True Crime: "[CASE NAME]: [CLIFFHANGER PHRASE] | True Crime" format.
    Max 70 characters. Must contain a hook.
    """
    is_true_crime = profile_name == "true_crime" or "crime" in niche.lower()

    if is_true_crime:
        prompt = (
            "You are a True Crime YouTube channel editor. Generate a compelling YouTube title.\n\n"
            "FORMAT: [CASE NAME]: [CLIFFHANGER PHRASE] | True Crime\n"
            "RULES:\n"
            "- Maximum 70 characters total\n"
            "- Must include a hook or cliffhanger, not just the case name\n"
            "- Examples: 'The Disappearance of Sarah Chen: What The Police Missed | True Crime'\n"
            "- Output ONLY the title, nothing else\n\n"
            f"SCRIPT EXCERPT:\n{script_text[:800]}\n\n"
            "TITLE:"
        )
    else:
        prompt = (
            "You are a YouTube SEO expert. Generate a compelling YouTube title.\n\n"
            "RULES:\n"
            "- Maximum 70 characters total\n"
            "- Include a strong hook or benefit\n"
            "- Output ONLY the title, nothing else\n\n"
            f"SCRIPT EXCERPT:\n{script_text[:800]}\n\n"
            "TITLE:"
        )

    try:
        title = _bedrock_invoke(prompt, max_tokens=100)
        title = title.strip('"').strip("'").strip()
        return title[:70]
    except Exception as exc:
        log.warning("SEO title generation failed: %s", exc)
        return niche.title()[:70]


def _generate_seo_description(
    script_text: str,
    profile_name: str,
    niche: str,
    act_timestamps: list[dict],
) -> str:
    """Generate a YouTube description via Claude Sonnet.

    First 150 chars: most compelling sentence (shown before 'show more').
    Includes timestamps for each Act, hashtags, and standard disclaimer.
    """
    is_true_crime = profile_name == "true_crime" or "crime" in niche.lower()
    hashtag_base = "#TrueCrime #ColdCase #Mystery" if is_true_crime else f"#{niche.replace(' ', '')}"

    prompt = (
        "You are a YouTube description writer. Create an engaging YouTube video description.\n\n"
        "REQUIREMENTS:\n"
        "- First sentence (max 150 chars): the most compelling/shocking line from the script\n"
        "- Then a brief 2-3 sentence overview (do not reveal the ending)\n"
        "- End with: 'Watch to find out what really happened.'\n"
        "- Do NOT include timestamps or hashtags — those will be added separately\n"
        "- Output ONLY the description body, nothing else\n\n"
        f"SCRIPT EXCERPT:\n{script_text[:1500]}\n\n"
        "DESCRIPTION:"
    )

    try:
        body = _bedrock_invoke(prompt, max_tokens=400)
    except Exception as exc:
        log.warning("SEO description generation failed: %s", exc)
        body = script_text[:150].strip()

    # Build timestamp section from act_timestamps
    timestamp_lines = []
    for act in act_timestamps:
        label = act.get("label", "")
        seconds = int(act.get("start_seconds", 0))
        mm = seconds // 60
        ss = seconds % 60
        if label:
            timestamp_lines.append(f"{mm:02d}:{ss:02d} — {label}")

    timestamp_block = "\n".join(timestamp_lines)

    disclaimer = (
        "\n\n⚠️ This video is for educational and informational purposes only.\n"
        "All information is sourced from public records."
    )

    parts = [body]
    if timestamp_block:
        parts.append(f"\n\n📌 CHAPTERS\n{timestamp_block}")
    parts.append(f"\n\n{hashtag_base} #Documentary #Unsolved")
    parts.append(disclaimer)

    return "".join(parts)[:5000]


def _generate_seo_tags(research_keywords: list[str], niche: str, profile_name: str) -> list[str]:
    """Generate YouTube tags from research keywords.

    Max 500 characters total including separating commas.
    Always includes: case-related terms, 'true crime', 'unsolved', 'documentary'.
    """
    base_tags = ["true crime", "unsolved", "documentary", "mystery", "investigation"]
    if "crime" not in niche.lower():
        base_tags = ["documentary", "investigation", niche.lower()]

    all_tags = base_tags + [kw.lower() for kw in research_keywords if kw]

    # Deduplicate preserving order
    seen = set()
    deduped = []
    for tag in all_tags:
        tag = tag.strip()
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)

    # Trim to 500 chars total
    result = []
    char_count = 0
    for tag in deduped:
        addition = len(tag) + (2 if result else 0)  # 2 for ", "
        if char_count + addition > 500:
            break
        result.append(tag)
        char_count += addition

    return result


def generate_seo_metadata(
    run_id: str,
    script_text: str,
    research_keywords: list[str],
    profile_name: str,
    niche: str,
    act_timestamps: list[dict],
    playlist_id: str = "",
) -> dict:
    """Generate complete YouTube SEO metadata before upload.

    Stores result at s3://nexus-outputs/{run_id}/metadata.json.
    Returns the metadata dict (never raises — logs and returns partial on error).
    """
    metadata: dict = {
        "run_id": run_id,
        "profile": profile_name,
        "niche": niche,
        "playlist_id": playlist_id,
    }

    try:
        metadata["title"] = _generate_seo_title(script_text, profile_name, niche)
    except Exception as exc:
        log.warning("[%s] SEO title error: %s", run_id, exc)
        metadata["title"] = niche.title()[:70]

    try:
        metadata["description"] = _generate_seo_description(
            script_text, profile_name, niche, act_timestamps
        )
    except Exception as exc:
        log.warning("[%s] SEO description error: %s", run_id, exc)
        metadata["description"] = ""

    try:
        metadata["tags"] = _generate_seo_tags(research_keywords, niche, profile_name)
    except Exception as exc:
        log.warning("[%s] SEO tags error: %s", run_id, exc)
        metadata["tags"] = []

    # Persist to S3
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/metadata.json",
            Body=json.dumps(metadata, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        log.info("[%s] SEO metadata stored at %s/metadata.json", run_id, run_id)
    except Exception as exc:
        log.warning("[%s] Failed to store metadata.json: %s", run_id, exc)

    return metadata


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


def _process_record(body: dict) -> dict:
    run_id: str = body["run_id"]
    video_s3_key: str = body["s3_key"]
    metadata: dict = body.get("metadata", {})
    dry_run: bool = metadata.get("dry_run", False)
    profile_name: str = metadata.get("profile", "documentary")
    niche: str = metadata.get("niche", "")
    primary_thumbnail_s3_key: str = metadata.get("primary_thumbnail_s3_key", "")
    thumbnail_s3_keys: list = metadata.get("thumbnail_s3_keys", [primary_thumbnail_s3_key])
    video_duration_sec: float = float(metadata.get("video_duration_sec", 0))

    step_start = notify_step_start("upload", run_id, niche=niche, profile=profile_name, dry_run=dry_run)

    # ── Generate SEO metadata (non-fatal) ────────────────────────────────────
    script_text: str = metadata.get("script_text", "")
    research_keywords: list = metadata.get("research_keywords", [])
    act_timestamps: list = metadata.get("act_timestamps", [])
    playlist_id: str = metadata.get("playlist_id", "")

    if script_text and not dry_run:
        try:
            seo = generate_seo_metadata(
                run_id=run_id,
                script_text=script_text,
                research_keywords=research_keywords,
                profile_name=profile_name,
                niche=niche,
                act_timestamps=act_timestamps,
                playlist_id=playlist_id,
            )
            # Prefer SEO-generated values if metadata didn't provide explicit ones
            if not metadata.get("title") or metadata.get("title") == "Untitled":
                metadata["title"] = seo.get("title", "Untitled")
            if not metadata.get("description"):
                metadata["description"] = seo.get("description", "")
            if not metadata.get("tags"):
                metadata["tags"] = seo.get("tags", [])
        except Exception as exc:
            log.warning("[%s] SEO metadata generation failed (non-fatal): %s", run_id, exc)

    title = metadata.get("title", "Untitled")
    description = metadata.get("description", "")
    tags = metadata.get("tags", [])

    if dry_run:
        log.info("[%s] DRY RUN mode — returning stub upload result", run_id)
        return {
            "run_id": run_id,
            "profile": profile_name,
            "niche": niche,
            "dry_run": True,
            "title": title,
            "video_id": "DRY_RUN_VIDEO_ID",
            "video_url": "https://youtube.com/watch?v=DRY_RUN_VIDEO_ID",
            "thumbnail_s3_keys": thumbnail_s3_keys,
            "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
            "final_video_s3_key": video_s3_key,
            "video_duration_sec": video_duration_sec,
        }

    auto_publish = os.environ.get("YOUTUBE_AUTO_PUBLISH", "false").lower() == "true"
    log.info("[%s] auto_publish=%s", run_id, auto_publish)

    if not auto_publish:
        log.info("[%s] Manual approval mode — saving pending_upload.json to S3", run_id)
        s3 = boto3.client("s3")
        pending = {
            "run_id": run_id,
            "profile": profile_name,
            "title": title,
            "description": description,
            "tags": tags,
            "final_video_s3_key": video_s3_key,
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

        elapsed = time.time() - step_start
        notify_step_complete("upload", run_id, [
            {"name": "Title", "value": title[:100], "inline": False},
            {"name": "Status", "value": "⏳ pending approval", "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0xF39C12)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "niche": niche,
            "dry_run": False,
            "video_id": "PENDING_MANUAL_APPROVAL",
            "video_url": "pending://manual-approval-required",
            "title": title,
            "thumbnail_s3_keys": thumbnail_s3_keys,
            "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
            "final_video_s3_key": video_s3_key,
            "video_duration_sec": video_duration_sec,
            "auto_publish": False,
        }

    log.info("[%s] Refreshing YouTube access token", run_id)
    if not primary_thumbnail_s3_key:
        raise ValueError("primary_thumbnail_s3_key is required in metadata for auto-publish mode")
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

    s3 = boto3.client("s3")
    with tempfile.TemporaryDirectory() as tmpdir:
        log.info("[%s] Downloading video from S3: %s", run_id, video_s3_key)
        video_local = os.path.join(tmpdir, "final_video.mp4")
        s3.download_file(S3_OUTPUTS_BUCKET, video_s3_key, video_local, Config=_S3_TRANSFER_CONFIG)

        log.info("[%s] Uploading video to YouTube", run_id)
        upload_result = _upload_video(video_local, video_metadata, access_token)
        video_id = upload_result.get("id", "")
        if not video_id:
            raise RuntimeError("YouTube upload returned no video ID")
        log.info("[%s] YouTube video ID: %s", run_id, video_id)

        log.info("[%s] Downloading thumbnail from S3: %s", run_id, primary_thumbnail_s3_key)
        thumbnail_local = os.path.join(tmpdir, "thumbnail.jpg")
        s3.download_file(S3_OUTPUTS_BUCKET, primary_thumbnail_s3_key, thumbnail_local)
        log.info("[%s] Setting thumbnail on YouTube", run_id)
        _upload_thumbnail(video_id, thumbnail_local, access_token)

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    elapsed = time.time() - step_start
    notify_step_complete("upload", run_id, [
        {"name": "Title", "value": title[:100], "inline": False},
        {"name": "YouTube URL", "value": video_url, "inline": False},
        {"name": "Profile", "value": profile_name, "inline": True},
    ], elapsed_sec=elapsed, dry_run=dry_run, color=0x2ECC71)

    return {
        "run_id": run_id,
        "profile": profile_name,
        "niche": niche,
        "dry_run": False,
        "video_id": video_id,
        "video_url": video_url,
        "title": title,
        "thumbnail_s3_keys": thumbnail_s3_keys,
        "primary_thumbnail_s3_key": primary_thumbnail_s3_key,
        "final_video_s3_key": video_s3_key,
        "video_duration_sec": video_duration_sec,
    }


def lambda_handler(event: dict, context) -> None:
    sfn = boto3.client("stepfunctions")
    for record in event["Records"]:
        body = json.loads(record["body"])
        run_id = body.get("run_id", "unknown")
        task_token = body["task_token"]
        try:
            result = _process_record(body)
            sfn.send_task_success(taskToken=task_token, output=json.dumps(result))
        except Exception as exc:
            log.error("[%s] Upload step FAILED: %s", run_id, exc, exc_info=True)
            _write_error(run_id, "upload", exc)
            sfn.send_task_failure(
                taskToken=task_token,
                error=type(exc).__name__,
                cause=str(exc),
            )

