import hashlib
import json
import os
import subprocess
import tempfile
import time
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


S3_ASSETS_BUCKET = "nexus-assets"
S3_OUTPUTS_BUCKET = "nexus-outputs"
FFMPEG_BIN = "/opt/bin/ffmpeg"
FFPROBE_BIN = "/opt/bin/ffprobe"

LUT_MAP = {
    "cinematic_warm": "luts/cinematic_teal_orange.cube",
    "cold_blue": "luts/cold_blue_corporate.cube",
    "clean_corporate": "luts/cold_blue_corporate.cube",
    "punchy_vibrant": "luts/punchy_vibrant_warm.cube",
    "vintage_sepia": "luts/vintage_sepia.cube",
    "high_contrast": "luts/high_contrast.cube",
}

VIGNETTE_DEFAULTS = {
    "documentary": "PI/2.8",
    "finance": "PI/6",
    "entertainment": "PI/5",
}

GRAIN_DEFAULTS = {
    "documentary": 5,
    "finance": 1,
    "entertainment": 2,
}


def _http_get(url: str, headers: dict | None = None, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _http_post(url: str, headers: dict, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


_clip_model = None


def _get_clip_model():
    global _clip_model
    if _clip_model is None:
        from sentence_transformers import SentenceTransformer
        _clip_model = SentenceTransformer("clip-ViT-B-32")
    return _clip_model


def _score_clip(local_path: str, query: str) -> float:
    try:
        from PIL import Image
        model = _get_clip_model()
        frame_path = local_path.replace(".mp4", "_frame.jpg")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", local_path, "-vf", "select=eq(n\\,30)", "-vframes", "1", frame_path],
            capture_output=True,
            check=False,
        )
        if not os.path.exists(frame_path):
            return 0.5
        img = Image.open(frame_path)
        import numpy as np
        embeddings = model.encode([img, query])
        a, b = embeddings
        cos = float(
            np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
        )
        return max(0.0, min(1.0, (cos + 1) / 2))
    except Exception:
        return 0.5


def _search_pexels(query: str, api_key: str, per_page: int = 5) -> list[str]:
    encoded = urllib.parse.quote(query)
    url = f"https://api.pexels.com/videos/search?query={encoded}&per_page={per_page}&orientation=landscape"
    headers = {"Authorization": api_key}
    try:
        data = json.loads(_http_get(url, headers=headers))
        urls = []
        for video in data.get("videos", []):
            for vf in video.get("video_files", []):
                if vf.get("quality") in ("hd", "sd") and vf.get("width", 0) >= 1280:
                    urls.append(vf["link"])
                    break
        return urls
    except Exception:
        return []


def _search_storyblocks(query: str, api_key: str, private_key: str, per_page: int = 5) -> list[str]:
    try:
        import hmac
        expires = str(int(time.time()) + 600)
        hmac_key = private_key + expires
        sig = hmac.new(hmac_key.encode(), api_key.encode(), hashlib.sha256).hexdigest()
        encoded = urllib.parse.quote(query)
        url = (
            f"https://api.storyblocks.com/api/v2/videos/search?"
            f"keywords={encoded}&num_results={per_page}&page_num=1"
            f"&APIKEY={api_key}&EXPIRES={expires}&HMAC={sig}"
        )
        data = json.loads(_http_get(url))
        urls = []
        for result in data.get("results", []):
            dl = result.get("preview_urls", {}).get("mp4_preview_url")
            if dl:
                urls.append(dl)
        return urls
    except Exception:
        return []


def _search_archive_org(query: str, per_page: int = 3) -> list[str]:
    encoded = urllib.parse.quote(query)
    url = (
        f"https://archive.org/advancedsearch.php?q={encoded}+mediatype:movies"
        f"&fl=identifier,title&rows={per_page}&output=json"
    )
    try:
        data = json.loads(_http_get(url))
        urls = []
        for doc in data.get("response", {}).get("docs", []):
            ident = doc.get("identifier")
            if ident:
                urls.append(f"https://archive.org/download/{ident}/{ident}.mp4")
        return urls
    except Exception:
        return []


def _generate_runway(prompt: str, api_key: str, tmpdir: str) -> str | None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Runway-Version": "2024-11-06",
    }
    try:
        body = {
            "model": "gen3a_turbo",
            "promptText": prompt,
            "duration": 5,
            "ratio": "1280:768",
        }
        response = _http_post("https://api.dev.runwayml.com/v1/image_to_video", headers=headers, body=body)
        task_id = response.get("id")
        if not task_id:
            return None

        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(5)
            poll_url = f"https://api.dev.runwayml.com/v1/tasks/{task_id}"
            req = urllib.request.Request(poll_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                task = json.loads(resp.read())
            status = task.get("status")
            if status == "SUCCEEDED":
                video_url = task.get("output", [None])[0]
                if video_url:
                    video_bytes = _http_get(video_url)
                    out_path = os.path.join(tmpdir, f"runway_{task_id}.mp4")
                    with open(out_path, "wb") as f:
                        f.write(video_bytes)
                    return out_path
                break
            elif status in ("FAILED", "CANCELLED"):
                break

        return None
    except Exception:
        return None


def _download_video(url: str, tmpdir: str, idx: int) -> str | None:
    try:
        video_bytes = _http_get(url)
        path = os.path.join(tmpdir, f"raw_{idx}.mp4")
        with open(path, "wb") as f:
            f.write(video_bytes)
        return path
    except Exception:
        return None


def _get_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
                "-show_streams", path,
            ],
            capture_output=True,
            check=True,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 10.0


def _build_camera_motion_filter(style: str) -> str:
    if style == "ken_burns_in":
        return (
            "zoompan=z='min(zoom+0.0008,1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            ":d=125:s=1920x1080:fps=25"
        )
    elif style == "ken_burns_out":
        return (
            "zoompan=z='if(lte(zoom,1.0),1.04,max(1.001,zoom-0.0008))'"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=125:s=1920x1080:fps=25"
        )
    elif style == "pan_left":
        return (
            "zoompan=z='1.08':x='iw/2-(iw/zoom/2)+t*3':y='ih/2-(ih/zoom/2)'"
            ":d=125:s=1920x1080:fps=25"
        )
    elif style == "pan_right":
        return (
            "zoompan=z='1.08':x='iw/2-(iw/zoom/2)-t*3':y='ih/2-(ih/zoom/2)'"
            ":d=125:s=1920x1080:fps=25"
        )
    else:
        return "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"


def _process_clip(
    raw_path: str,
    section: dict,
    profile: dict,
    profile_name: str,
    lut_local_path: str | None,
    output_path: str,
) -> bool:
    try:
        target_dur = profile.get("visuals", {}).get("avg_clip_duration_sec", 5.0)
        clip_dur = min(_get_duration(raw_path), target_dur)
        camera_style = section.get("visual_cue", {}).get("camera_style", "static")
        vignette_angle = VIGNETTE_DEFAULTS.get(profile_name, "PI/4")
        grain_strength = GRAIN_DEFAULTS.get(profile_name, 2)

        filters = []

        filters.append(_build_camera_motion_filter(camera_style))

        if lut_local_path and os.path.exists(lut_local_path):
            filters.append(f"lut3d=file='{lut_local_path}'")

        filters.append(f"vignette=angle={vignette_angle}:mode=backward")

        filters.append(f"noise=alls={grain_strength}:allf=t+u")

        filters.append("unsharp=5:5:1.0:5:5:0.0")

        vf = ",".join(filters)

        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", "0",
            "-i", raw_path,
            "-t", str(clip_dur),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-an",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return True
    except Exception:
        return False


def _source_and_process_section(
    section: dict,
    section_idx: int,
    profile: dict,
    profile_name: str,
    lut_local_path: str | None,
    tmpdir: str,
    s3,
    run_id: str,
    has_storyblocks: bool,
    has_runway: bool,
    storyblocks_key: str,
    storyblocks_private: str,
    pexels_key: str,
    runway_key: str,
) -> dict | None:
    visual_cue = section.get("visual_cue", {})
    queries = visual_cue.get("search_queries", [section.get("title", "nature landscape")])
    threshold = profile.get("visuals", {}).get("clip_score_threshold", 0.65)
    archive_enabled = profile.get("visuals", {}).get("source_priority", [])

    candidates: list[str] = []

    for query in queries[:2]:
        if has_storyblocks:
            candidates += _search_storyblocks(query, storyblocks_key, storyblocks_private)
        candidates += _search_pexels(query, pexels_key)
        if "archive_org" in archive_enabled:
            candidates += _search_archive_org(query)

    best_path: str | None = None
    best_score: float = 0.0

    for idx, url in enumerate(candidates[:8]):
        raw_path = _download_video(url, tmpdir, f"{section_idx}_{idx}")
        if raw_path is None:
            continue
        score = _score_clip(raw_path, " ".join(queries))
        if score > best_score:
            best_score = score
            best_path = raw_path
        if score >= threshold:
            break

    if (best_score < threshold or best_path is None) and has_runway:
        runway_path = _generate_runway(" ".join(queries[:1]), runway_key, tmpdir)
        if runway_path:
            runway_score = _score_clip(runway_path, " ".join(queries))
            if runway_score > best_score:
                best_score = runway_score
                best_path = runway_path

    if best_path is None:
        return None

    out_filename = f"section_{section_idx:03d}.mp4"
    out_path = os.path.join(tmpdir, out_filename)
    success = _process_clip(best_path, section, profile, profile_name, lut_local_path, out_path)
    if not success:
        return None

    s3_key = f"{run_id}/clips/{out_filename}"
    s3.upload_file(out_path, S3_ASSETS_BUCKET, s3_key)

    return {
        "section_idx": section_idx,
        "clip_s3_key": s3_key,
        "score": best_score,
        "camera_style": visual_cue.get("camera_style", "static"),
        "transition_in": visual_cue.get("transition_in", "dissolve"),
        "overlay_type": visual_cue.get("overlay_type", "none"),
        "overlay_text": visual_cue.get("overlay_text", ""),
        "color_grade": visual_cue.get("color_grade", "cinematic_warm"),
        "duration_estimate_sec": section.get("duration_estimate_sec", 5),
        "emotion": section.get("emotion", "neutral"),
    }


def _download_lut(color_grade: str, tmpdir: str, s3) -> str | None:
    lut_s3_key = LUT_MAP.get(color_grade)
    if not lut_s3_key:
        return None
    local_path = os.path.join(tmpdir, os.path.basename(lut_s3_key))
    try:
        s3.download_file(S3_ASSETS_BUCKET, lut_s3_key, local_path)
        return local_path
    except Exception:
        return None


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


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    mixed_audio_s3_key: str = event.get("mixed_audio_s3_key", "")
    total_duration_estimate: float = float(event.get("total_duration_estimate", 0))

    try:
        s3 = boto3.client("s3")

        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        profile_obj = s3.get_object(Bucket="nexus-config", Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        sections: list[dict] = script.get("sections", [])

        if dry_run:
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "mixed_audio_s3_key": mixed_audio_s3_key,
                "total_duration_estimate": total_duration_estimate,
                "sections": [
                    {
                        "section_idx": i,
                        "clip_s3_key": f"{run_id}/clips/section_{i:03d}_dry_run.mp4",
                        "score": 0.9,
                        "transition_in": "dissolve",
                        "overlay_type": "none",
                        "overlay_text": "",
                        "duration_estimate_sec": s.get("duration_estimate_sec", 5),
                    }
                    for i, s in enumerate(sections)
                ],
            }

        pexels_key = get_secret("nexus/pexels_api_key")["api_key"]

        storyblocks_key = ""
        storyblocks_private = ""
        has_storyblocks = False
        try:
            sb_secret = get_secret("nexus/storyblocks_api_key")
            storyblocks_key = sb_secret.get("api_key", "")
            storyblocks_private = sb_secret.get("private_key", "")
            has_storyblocks = bool(storyblocks_key)
        except Exception:
            pass

        runway_key = ""
        has_runway = False
        try:
            runway_key = get_secret("nexus/runwayml_api_key")["api_key"]
            has_runway = bool(runway_key)
        except Exception:
            pass

        color_grade_default = profile.get("visuals", {}).get("color_grade_default", "cinematic_warm")

        with tempfile.TemporaryDirectory() as tmpdir:
            lut_local_path = _download_lut(color_grade_default, tmpdir, s3)

            processed_sections = []
            for idx, section in enumerate(sections):
                result = _source_and_process_section(
                    section=section,
                    section_idx=idx,
                    profile=profile,
                    profile_name=profile_name,
                    lut_local_path=lut_local_path,
                    tmpdir=tmpdir,
                    s3=s3,
                    run_id=run_id,
                    has_storyblocks=has_storyblocks,
                    has_runway=has_runway,
                    storyblocks_key=storyblocks_key,
                    storyblocks_private=storyblocks_private,
                    pexels_key=pexels_key,
                    runway_key=runway_key,
                )
                if result:
                    processed_sections.append(result)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "script_s3_key": script_s3_key,
            "mixed_audio_s3_key": mixed_audio_s3_key,
            "total_duration_estimate": total_duration_estimate,
            "sections": processed_sections,
            "title": script.get("title", ""),
            "mood": script.get("mood", ""),
        }

    except Exception as exc:
        _write_error(run_id, "visuals", exc)
        raise
