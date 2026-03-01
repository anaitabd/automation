import base64
import json
import os
import subprocess
import tempfile
import time
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


S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")


def _find_bin(name: str) -> str:
    """Locate a binary (ffmpeg / ffprobe) across Lambda-layer and system paths."""
    for candidate in (f"/opt/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    import shutil
    path = shutil.which(name)
    if path:
        return path
    raise FileNotFoundError(f"{name} not found. Install it or set the {name.upper()}_BIN env var.")


FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or _find_bin("ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or _find_bin("ffprobe")
BEDROCK_MODEL_ID = "us.anthropic.claude-3-sonnet-20240229-v1:0"


def _http_post(url: str, headers: dict, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            merged = {"User-Agent": "NexusCloud/1.0"}
            merged.update(headers)
            req = urllib.request.Request(url, data=data, headers=merged, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _get_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 60.0


def _extract_keyframes(video_path: str, tmpdir: str, n: int = 6) -> list[str]:
    duration = _get_duration(video_path)
    start = duration * 0.10
    end = duration * 0.90
    usable = end - start
    if usable <= 0:
        start, usable = 0, duration

    frame_paths = []
    for i in range(n):
        ts = start + (usable / (n - 1)) * i if n > 1 else start + usable / 2
        out_path = os.path.join(tmpdir, f"frame_{i:02d}.jpg")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-ss", str(ts), "-i", video_path,
             "-vframes", "1", "-q:v", "2", out_path],
            capture_output=True, check=False,
        )
        if os.path.exists(out_path):
            frame_paths.append(out_path)

    return frame_paths


def _score_frame_bedrock(frame_path: str) -> float:
    with open(frame_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    client = boto3.client("bedrock-runtime")
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Rate this YouTube thumbnail frame on a scale of 0.0 to 1.0 based on: "
                                "contrast, subject clarity, emotional impact, and legibility at small size. "
                                "Respond with ONLY a JSON object: {\"score\": 0.0}"
                            ),
                        },
                    ],
                }
            ],
        }
    )
    try:
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        raw = json.loads(response["body"].read())["content"][0]["text"]
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(raw)
        return float(data.get("score", 0.5))
    except Exception:
        return 0.5


def _generate_thumbnail_concepts(
    title: str, mood: str, accent_color: str
) -> list[dict]:
    client = boto3.client("bedrock-runtime")
    prompt = (
        f"You are a YouTube thumbnail strategist. Create 3 distinct thumbnail concepts for:\n"
        f"Title: {title}\nMood: {mood}\nAccent color: {accent_color}\n\n"
        "For each concept provide:\n"
        "- top_text: max 4 words, ALL CAPS, high-impact\n"
        "- sub_text: max 6 words, title case\n"
        "- emotion_trigger: one word (fear/curiosity/excitement/awe/shock)\n"
        "- color_scheme: one of (dark_dramatic/bright_energetic/cinematic_cold/warm_epic)\n\n"
        "Return ONLY a JSON array of 3 objects with these exact keys. No markdown."
    )
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    retries = 3
    for attempt in range(retries):
        try:
            response = client.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            raw = json.loads(response["body"].read())["content"][0]["text"]
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            concepts = json.loads(raw)
            return concepts[:3]
        except Exception:
            if attempt == retries - 1:
                return [
                    {"top_text": "UNTOLD STORY", "sub_text": title[:30], "emotion_trigger": "curiosity", "color_scheme": "dark_dramatic"},
                    {"top_text": "SHOCKING TRUTH", "sub_text": title[:30], "emotion_trigger": "shock", "color_scheme": "cinematic_cold"},
                    {"top_text": "YOU WON'T BELIEVE", "sub_text": title[:30], "emotion_trigger": "awe", "color_scheme": "warm_epic"},
                ]
            time.sleep(2 ** attempt)
    return []


def _render_thumbnail(
    frame_path: str,
    concept: dict,
    profile: dict,
    tmpdir: str,
    idx: int,
) -> str:
    out_path = os.path.join(tmpdir, f"thumbnail_{idx}.jpg")
    accent_color = profile.get("thumbnail", {}).get("accent_color", "#C8A96E")
    channel_name = profile.get("name", "Nexus").upper()

    top_text = concept.get("top_text", "")[:45].replace("'", "\\'").replace(":", "\\:")
    sub_text = concept.get("sub_text", "")[:45].replace("'", "\\'").replace(":", "\\:")

    vf_parts = [
        "eq=contrast=1.15:saturation=1.25:brightness=0.02",
        (
            "drawbox=x=0:y=ih*0.55:width=iw:height=ih*0.45"
            ":color=black@0.75:t=fill"
        ),
        (
            f"drawtext=text='{top_text}'"
            ":fontcolor=white:fontsize=88"
            ":x=(w-text_w)/2:y=40"
            ":shadowcolor=black@0.9:shadowx=3:shadowy=3"
            ":bordercolor=black:borderw=2"
        ),
        (
            f"drawtext=text='{sub_text}'"
            ":fontcolor=#DDDDDD:fontsize=52"
            ":x=(w-text_w)/2:y=ih*0.60"
            ":shadowcolor=black@0.9:shadowx=2:shadowy=2"
        ),
        (
            f"drawbox=x=iw-280:y=10:width=270:height=60"
            f":color={accent_color}@0.9:t=fill,"
            f"drawtext=text='{channel_name}'"
            ":fontcolor=white:fontsize=24"
            ":x=iw-270:y=28"
        ),
    ]

    cmd = [
        FFMPEG_BIN, "-y",
        "-i", frame_path,
        "-vf", ",".join(vf_parts),
        "-vframes", "1",
        "-q:v", "2",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


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
                "title": f"🖼️ Nexus Cloud — {step}",
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
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    title_passthrough: str = event.get("title", "")
    video_duration_sec: float = float(event.get("video_duration_sec", 0))

    try:
        s3 = boto3.client("s3")

        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        title = script.get("title", "") or title_passthrough
        mood = script.get("mood", "neutral")
        accent_color = profile.get("thumbnail", {}).get("accent_color", "#C8A96E")

        if dry_run:
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "title": title,
                "final_video_s3_key": final_video_s3_key,
                "video_duration_sec": video_duration_sec,
                "thumbnail_s3_keys": [
                    f"{run_id}/thumbnails/thumbnail_0.jpg",
                    f"{run_id}/thumbnails/thumbnail_1.jpg",
                    f"{run_id}/thumbnails/thumbnail_2.jpg",
                ],
                "primary_thumbnail_s3_key": f"{run_id}/thumbnails/thumbnail_0.jpg",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            video_local = os.path.join(tmpdir, "final_video.mp4")
            s3.download_file(S3_OUTPUTS_BUCKET, final_video_s3_key, video_local)

            frames = _extract_keyframes(video_local, tmpdir, n=6)

            scores = [_score_frame_bedrock(f) for f in frames]
            best_frame_idx = scores.index(max(scores))
            best_frame = frames[best_frame_idx]

            concepts = _generate_thumbnail_concepts(title, mood, accent_color)
            if len(concepts) < 3:
                concepts += [concepts[0]] * (3 - len(concepts))

            thumbnail_local_paths = []
            for i, concept in enumerate(concepts[:3]):
                t_path = _render_thumbnail(best_frame, concept, profile, tmpdir, i)
                thumbnail_local_paths.append(t_path)

            thumbnail_s3_keys = []
            for i, t_path in enumerate(thumbnail_local_paths):
                key = f"{run_id}/thumbnails/thumbnail_{i}.jpg"
                s3.upload_file(t_path, S3_OUTPUTS_BUCKET, key)
                thumbnail_s3_keys.append(key)

        _notify_discord("Thumbnails Generated", 0xF1C40F, run_id, [
            {"name": "Title", "value": title[:100], "inline": False},
            {"name": "Best Score", "value": f"{max(scores) if scores else 0.0:.2f}", "inline": True},
            {"name": "Variants", "value": str(len(thumbnail_s3_keys)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], dry_run=dry_run)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "script_s3_key": script_s3_key,
            "title": title,
            "final_video_s3_key": final_video_s3_key,
            "video_duration_sec": video_duration_sec,
            "thumbnail_s3_keys": thumbnail_s3_keys,
            "primary_thumbnail_s3_key": thumbnail_s3_keys[0],
            "frame_scores": scores,
            "best_frame_score": max(scores) if scores else 0.0,
        }

    except Exception as exc:
        _write_error(run_id, "thumbnail", exc)
        raise
