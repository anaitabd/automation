import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-visuals")

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

MIN_VIDEO_BYTES = 200_000


def _http_get(url: str, headers: dict | None = None, retries: int = 3) -> bytes:
    merged = {"User-Agent": "NexusCloud/1.0"}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
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
            merged = {"User-Agent": "NexusCloud/1.0"}
            merged.update(headers)
            req = urllib.request.Request(url, data=data, headers=merged, method="POST")
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
        if not _has_video_stream(local_path):
            return 0.0
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
        img.close()
        # Clean up frame to save disk space
        try:
            os.remove(frame_path)
        except OSError:
            pass
        a, b = embeddings
        cos = float(
            np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
        )
        return max(0.0, min(1.0, (cos + 1) / 2))
    except Exception:
        return 0.5


def _is_vimeo_link(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return "vimeo.com" in host


def _pick_best_pexels_file(video_files: list[dict]) -> str | None:
    # Prefer MP4 + non-Vimeo, then largest width.
    candidates: list[tuple[int, bool, str]] = []
    for vf in video_files:
        link = vf.get("link")
        if not link:
            continue
        file_type = (vf.get("file_type") or "").lower()
        if file_type and "mp4" not in file_type:
            continue
        width = int(vf.get("width", 0))
        is_vimeo = _is_vimeo_link(link)
        candidates.append((width, is_vimeo, link))
    if not candidates:
        return None
    # Sort by width desc, prefer non-Vimeo (False < True).
    candidates.sort(key=lambda item: (item[0], not item[1]), reverse=True)
    return candidates[0][2]


def _search_pexels(query: str, api_key: str, per_page: int = 8) -> list[str]:
    log.info("Pexels search: query=%r per_page=%d", query, per_page)
    encoded = urllib.parse.quote(query)
    url = f"https://api.pexels.com/videos/search?query={encoded}&per_page={per_page}&orientation=landscape&size=large"
    headers = {"Authorization": api_key}
    try:
        data = json.loads(_http_get(url, headers=headers))
        urls = []
        for video in data.get("videos", []):
            best_file = _pick_best_pexels_file(video.get("video_files", []))
            if best_file:
                urls.append(best_file)
        log.info("Pexels returned %d usable videos", len(urls))
        return urls
    except Exception as exc:
        log.warning("Pexels search failed: %s", exc)
        return []


def _search_archive_org(query: str, per_page: int = 5) -> list[str]:
    log.info("Archive.org search: query=%r", query)
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
                best_url = _archive_org_best_mp4_url(ident)
                if best_url:
                    urls.append(best_url)
        log.info("Archive.org returned %d candidates", len(urls))
        return urls
    except Exception as exc:
        log.warning("Archive.org search failed: %s", exc)
        return []


def _archive_org_best_mp4_url(identifier: str) -> str | None:
    try:
        meta = json.loads(_http_get(f"https://archive.org/metadata/{identifier}"))
        files = meta.get("files", [])
        candidates: list[tuple[int, str]] = []
        for file_info in files:
            name = file_info.get("name") or ""
            if not name.lower().endswith(".mp4"):
                continue
            size = int(file_info.get("size") or 0)
            candidates.append((size, name))
        if not candidates:
            return None
        _, best_name = max(candidates, key=lambda item: item[0])
        quoted = urllib.parse.quote(best_name)
        return f"https://archive.org/download/{identifier}/{quoted}"
    except Exception as exc:
        log.warning("Archive.org metadata failed for %s: %s", identifier, exc)
        return None


def _download_video(url: str, tmpdir: str, idx: int | str, pexels_key: str | None = None) -> str | None:
    headers = {"User-Agent": "NexusCloud/1.0", "Accept": "video/*"}
    netloc = urllib.parse.urlparse(url).netloc.lower()
    if pexels_key and "pexels.com" in netloc:
        headers["Authorization"] = pexels_key
    if pexels_key and "vimeo.com" in netloc:
        headers["Referer"] = "https://www.pexels.com"
        headers["Origin"] = "https://www.pexels.com"
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(3):
        try:
            log.info("Downloading video %s …", idx)
            with urllib.request.urlopen(req, timeout=90) as resp:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                content_len = resp.headers.get("Content-Length")
                if resp.status not in (200, 206):
                    raise urllib.error.HTTPError(url, resp.status, "bad status", resp.headers, None)
                if content_type and not content_type.startswith("video/") and content_type != "application/octet-stream":
                    raise ValueError(f"unexpected content-type {content_type}")
                if content_len and int(content_len) < MIN_VIDEO_BYTES:
                    raise ValueError(f"download too small ({content_len} bytes)")
                video_bytes = resp.read()
            if len(video_bytes) < MIN_VIDEO_BYTES:
                raise ValueError(f"download too small ({len(video_bytes)} bytes)")
            path = os.path.join(tmpdir, f"raw_{idx}.mp4")
            with open(path, "wb") as f:
                f.write(video_bytes)
            log.info("Downloaded video %s (%.1f MB)", idx, len(video_bytes) / 1_048_576)
            return path
        except urllib.error.HTTPError as exc:
            if attempt == 2:
                log.warning(
                    "Download failed for video %s (%s): HTTP %d",
                    idx,
                    netloc or "unknown",
                    exc.code,
                )
                return None
            time.sleep(2 ** attempt)
        except Exception as exc:
            if attempt == 2:
                log.warning("Download failed for video %s (%s): %s", idx, netloc or "unknown", exc)
                return None
            time.sleep(2 ** attempt)
    return None


def _has_video_stream(path: str) -> bool:
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type",
                "-print_format", "json",
                path,
            ],
            capture_output=True,
            check=True,
        )
        data = json.loads(result.stdout)
        return bool(data.get("streams"))
    except Exception:
        return False


def _has_motion(path: str) -> bool:
    try:
        result = subprocess.run(
            [
                FFMPEG_BIN, "-i", path,
                "-vf", "select=not(mod(n\\,10)),signalstats",
                "-frames:v", "5",
                "-f", "null", "-",
            ],
            capture_output=True, check=False, timeout=30,
        )
        stderr = result.stderr.decode("utf-8", errors="replace")
        import re
        yavg_values = [float(m.group(1)) for m in re.finditer(r"YAVG:(\d+\.?\d*)", stderr)]
        if len(yavg_values) < 2:
            return True
        return (max(yavg_values) - min(yavg_values)) >= 1.0
    except Exception:
        return True


def _get_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
                "-show_streams", "-show_format", path,
            ],
            capture_output=True,
            check=True,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            return float(dur)
    except Exception:
        pass
    return 0.0


def _build_camera_motion_filter(style: str, clip_dur: float) -> str:
    """Build video-safe camera motion filters.

    Uses scale + animated crop instead of zoompan, because zoompan is designed
    for still-image input and fails (exit 234) on video streams.
    """
    clip_dur = max(0.5, clip_dur)
    if style == "ken_burns_in":
        # Slow zoom-in: start at full frame, crop inward over time
        return (
            "scale=2048:1152,"
            f"crop=w='2048-128*t/{clip_dur}':h='1152-72*t/{clip_dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            "scale=1920:1080"
        )
    elif style == "ken_burns_out":
        # Slow zoom-out: start cropped, expand over time
        return (
            "scale=2048:1152,"
            f"crop=w='1920+128*t/{clip_dur}':h='1080+72*t/{clip_dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            "scale=1920:1080"
        )
    elif style == "pan_left":
        # Pan from right to left
        return (
            "scale=2160:1080:force_original_aspect_ratio=increase,"
            "scale='max(2160,iw)':'max(1080,ih)',"
            f"crop=1920:1080:x='min(iw-1920,max(0,(iw-1920)*t/{clip_dur}))':y='(ih-1080)/2'"
        )
    elif style == "pan_right":
        # Pan from left to right
        return (
            "scale=2160:1080:force_original_aspect_ratio=increase,"
            "scale='max(2160,iw)':'max(1080,ih)',"
            f"crop=1920:1080:x='max(0,(iw-1920)*(1-t/{clip_dur}))':y='(ih-1080)/2'"
        )
    elif style == "slow_drift":
        # Subtle diagonal drift — scale up then slowly pan diagonally
        return (
            "scale=2160:1216:force_original_aspect_ratio=increase,"
            "scale='max(2160,iw)':'max(1216,ih)',"
            f"crop=1920:1080:x='min(iw-1920,max(0,(iw-1920)*t/{clip_dur}*0.6))'"
            f":y='min(ih-1080,max(0,(ih-1080)*t/{clip_dur}*0.4))'"
        )
    elif style == "dolly_in":
        # Faster zoom-in simulating a dolly push
        return (
            "scale=2304:1296,"
            f"crop=w='2304-(384*t/{clip_dur})':h='1296-(216*t/{clip_dur})'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            "scale=1920:1080"
        )
    else:
        import random
        motion_styles = ["ken_burns_in", "ken_burns_out", "slow_drift", "pan_left", "pan_right"]
        chosen = random.choice(motion_styles)
        return _build_camera_motion_filter(chosen, clip_dur)


def _process_clip(
    raw_path: str,
    section: dict,
    profile: dict,
    profile_name: str,
    lut_local_path: str | None,
    output_path: str,
    target_duration: float | None = None,
) -> bool:
    try:
        if not _has_video_stream(raw_path):
            log.warning("Skipping invalid video stream: %s", os.path.basename(raw_path))
            return False
        raw_dur = _get_duration(raw_path)
        if raw_dur <= 0.5:
            log.warning("Skipping short/unknown duration clip: %s", os.path.basename(raw_path))
            return False
        # Use target_duration if specified, otherwise use profile default
        max_dur = target_duration or profile.get("visuals", {}).get("avg_clip_duration_sec", 8.0)
        clip_dur = min(raw_dur, max_dur)
        log.info("Processing clip %s → %s (%.1fs, camera=%s)",
                 os.path.basename(raw_path), os.path.basename(output_path),
                 clip_dur, section.get("visual_cue", {}).get("camera_style", "static"))
        camera_style = section.get("visual_cue", {}).get("camera_style", "static")
        vignette_angle = VIGNETTE_DEFAULTS.get(profile_name, "PI/4")
        grain_strength = GRAIN_DEFAULTS.get(profile_name, 2)

        filters = []

        filters.append(_build_camera_motion_filter(camera_style, clip_dur))

        if lut_local_path and os.path.exists(lut_local_path):
            filters.append(f"lut3d=file='{lut_local_path}'")

        filters.append(f"vignette=angle={vignette_angle}:mode=backward")

        filters.append(f"noise=alls={grain_strength}:allf=t+u")

        # Sharpening — slightly stronger for cinematic crispness
        filters.append("unsharp=5:5:1.2:5:5:0.0")

        # Subtle fade-in / fade-out on each clip for smoother assembly
        filters.append(f"fade=t=in:st=0:d=0.3,fade=t=out:st={max(0.0, clip_dur - 0.3)}:d=0.3")

        vf = ",".join(filters)

        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", "0",
            "-i", raw_path,
            "-t", str(clip_dur),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "16",
            "-pix_fmt", "yuv420p",
            "-an",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        log.info("FFmpeg done: %s", os.path.basename(output_path))
        return True
    except subprocess.CalledProcessError as exc:
        stderr_msg = exc.stderr.decode("utf-8", errors="replace")[-500:] if exc.stderr else "no stderr"
        log.warning("FFmpeg failed for %s (exit %d): %s", os.path.basename(output_path), exc.returncode, stderr_msg)
        return False
    except Exception as exc:
        log.warning("FFmpeg failed for %s: %s", os.path.basename(output_path), exc)
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
    pexels_key: str,
) -> dict | None:
    visual_cue = section.get("visual_cue", {})
    queries = visual_cue.get("search_queries", [section.get("title", "nature landscape")])
    threshold = profile.get("visuals", {}).get("clip_score_threshold", 0.65)
    archive_enabled = profile.get("visuals", {}).get("source_priority", [])
    clips_needed = max(1, int(visual_cue.get("clips_needed", 2)))
    section_duration = float(section.get("duration_estimate_sec", 30))
    per_clip_dur = max(3.0, section_duration / clips_needed)

    log.info("── Section %d: %d clips needed, %.0fs duration, queries=%s",
             section_idx, clips_needed, section_duration, queries[:2])

    # ── Gather candidates from all queries (use up to 3 queries, 5 results each) ──
    candidates: list[str] = []
    for query in queries[:3]:
        candidates += _search_pexels(query, pexels_key, per_page=5)
        if "archive_org" in archive_enabled:
            candidates += _search_archive_org(query, per_page=3)

    # Deduplicate URLs
    seen_urls: set[str] = set()
    unique_candidates: list[str] = []
    for url in candidates:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_candidates.append(url)
    candidates = unique_candidates

    max_candidates = int(os.environ.get("VISUALS_MAX_CANDIDATES", 8))
    log.info("Section %d: %d unique candidates to score (max %d)", section_idx, len(candidates), max_candidates)

    # ── Score and rank all candidates ──
    scored: list[tuple[float, str]] = []
    query_text = " ".join(queries)
    for idx, url in enumerate(candidates[:max_candidates]):
        raw_path = _download_video(url, tmpdir, f"{section_idx}_{idx}", pexels_key=pexels_key)
        if raw_path is None:
            continue
        if not _has_motion(raw_path):
            log.info("Section %d candidate %d rejected: no motion detected", section_idx, idx)
            continue
        score = _score_clip(raw_path, query_text)
        scored.append((score, raw_path))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)
    log.info("Section %d: %d clips scored, best=%.2f, threshold=%.2f",
             section_idx, len(scored), scored[0][0] if scored else 0, threshold)

    # Take the top N clips over threshold
    selected = [clip for clip in scored if clip[0] >= threshold][:clips_needed]

    if not selected:
        return None

    # ── Process and upload each clip ──
    clip_s3_keys: list[str] = []
    best_score = 0.0
    for clip_idx, (score, raw_path) in enumerate(selected):
        best_score = max(best_score, score)
        out_filename = f"section_{section_idx:03d}_{clip_idx:02d}.mp4"
        out_path = os.path.join(tmpdir, out_filename)
        success = _process_clip(
            raw_path, section, profile, profile_name,
            lut_local_path, out_path,
            target_duration=per_clip_dur,
        )
        if not success:
            continue
        s3_key = f"{run_id}/clips/{out_filename}"
        s3.upload_file(out_path, S3_ASSETS_BUCKET, s3_key)
        clip_s3_keys.append(s3_key)

    if not clip_s3_keys:
        log.warning("Section %d: no clips produced!", section_idx)
        return None

    log.info("Section %d: uploaded %d clips to S3", section_idx, len(clip_s3_keys))
    return {
        "section_idx": section_idx,
        "clip_s3_key": clip_s3_keys[0],            # backward compat
        "clip_s3_keys": clip_s3_keys,               # multi-clip support
        "score": best_score,
        "clips_sourced": len(clip_s3_keys),
        "camera_style": visual_cue.get("camera_style", "static"),
        "transition_in": visual_cue.get("transition_in", "dissolve"),
        "overlay_type": visual_cue.get("overlay_type", "none"),
        "overlay_text": visual_cue.get("overlay_text", ""),
        "color_grade": visual_cue.get("color_grade", "cinematic_warm"),
        "duration_estimate_sec": section.get("duration_estimate_sec", 30),
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


SCRATCH_DIR = os.environ.get("TMPDIR", "/mnt/scratch")


def lambda_handler(event: dict, context) -> dict:
    handler_start = time.time()
    deadline = handler_start + 7140  # 119 min — leaves buffer before 2h Fargate task timeout

    run_id: str = event.get("run_id") or os.environ.get("RUN_ID", "")
    profile_name: str = event.get("profile") or os.environ.get("PROFILE", "documentary")
    niche: str = event.get("niche") or os.environ.get("NICHE", "")
    script_s3_key: str = event.get("script_s3_key") or os.environ.get("SCRIPT_S3_KEY", "")
    dry_run_raw = event.get("dry_run") if "dry_run" in event else os.environ.get("DRY_RUN", "false")
    dry_run: bool = dry_run_raw if isinstance(dry_run_raw, bool) else str(dry_run_raw).lower() == "true"
    mixed_audio_s3_key: str = event.get("mixed_audio_s3_key") or os.environ.get("MIXED_AUDIO_S3_KEY", "")
    total_duration_estimate: float = float(event.get("total_duration_estimate") or os.environ.get("TOTAL_DURATION_ESTIMATE", 0))

    step_start = notify_step_start("visuals", run_id, niche=niche, profile=profile_name, dry_run=dry_run)

    try:
        s3 = boto3.client("s3")

        log.info("Loading script from s3://%s/%s", S3_OUTPUTS_BUCKET, script_s3_key)
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        sections: list[dict] = script.get("sections", [])
        log.info("Visuals pipeline starting — %d sections, profile=%s, dry_run=%s",
                 len(sections), profile_name, dry_run)

        if dry_run:
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "mixed_audio_s3_key": mixed_audio_s3_key,
                "total_duration_estimate": total_duration_estimate,
                "title": script.get("title", event.get("title", "")),
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

        color_grade_default = profile.get("visuals", {}).get("color_grade_default", "cinematic_warm")

        with tempfile.TemporaryDirectory(dir=SCRATCH_DIR if os.path.isdir(SCRATCH_DIR) else None) as tmpdir:
            lut_local_path = _download_lut(color_grade_default, tmpdir, s3)

            # ── Process sections in parallel to stay within the 15-min timeout ──
            max_workers = min(len(sections), int(os.environ.get("VISUALS_PARALLELISM", 4)))
            processed_sections: list[dict] = []

            def _process_one(idx_section: tuple[int, dict]) -> tuple[int, dict | None, float]:
                idx, section = idx_section
                # Each thread gets its own subdirectory to avoid file collisions
                section_dir = os.path.join(tmpdir, f"section_{idx:03d}")
                os.makedirs(section_dir, exist_ok=True)
                # Each thread needs its own S3 client (boto3 clients aren't thread-safe)
                thread_s3 = boto3.client("s3")
                log.info("━━ Processing section %d/%d ━━", idx + 1, len(sections))
                section_start = time.time()
                result = _source_and_process_section(
                    section=section,
                    section_idx=idx,
                    profile=profile,
                    profile_name=profile_name,
                    lut_local_path=lut_local_path,
                    tmpdir=section_dir,
                    s3=thread_s3,
                    run_id=run_id,
                    pexels_key=pexels_key,
                )
                elapsed = time.time() - section_start
                log.info("Section %d/%d done in %.1fs (success=%s)",
                         idx + 1, len(sections), elapsed, result is not None)
                # Disk cleanup for this section's temp files
                for fname in os.listdir(section_dir):
                    fpath = os.path.join(section_dir, fname)
                    if os.path.isfile(fpath) and fname != os.path.basename(lut_local_path or ""):
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass
                return (idx, result, elapsed)

            log.info("Processing %d sections with %d parallel workers (deadline in %.0fs)",
                     len(sections), max_workers, deadline - time.time())
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_process_one, (idx, section)): idx
                    for idx, section in enumerate(sections)
                }
                # Use remaining time as timeout so we return partial results
                # instead of being hard-killed by the Lambda runtime
                remaining = max(10, deadline - time.time())
                try:
                    for future in as_completed(futures, timeout=remaining):
                        idx, result, elapsed = future.result()
                        if result:
                            processed_sections.append(result)
                except TimeoutError:
                    done_count = sum(1 for f in futures if f.done())
                    log.warning("⏰ Deadline approaching — returning %d/%d sections "
                                "(completed %d futures)", len(processed_sections),
                                len(sections), done_count)
                    for f in futures:
                        f.cancel()

            # Sort by section index to maintain order
            processed_sections.sort(key=lambda s: s["section_idx"])

        log.info("Visuals complete — %d/%d sections produced clips", len(processed_sections), len(sections))

        sections_key = f"{run_id}/status/visuals_sections.json"
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=sections_key,
            Body=json.dumps(processed_sections).encode("utf-8"),
            ContentType="application/json",
        )
        log.info("Sections metadata written to s3://%s/%s", S3_OUTPUTS_BUCKET, sections_key)

        elapsed = time.time() - step_start
        notify_step_complete("visuals", run_id, [
            {"name": "Clips Processed", "value": str(len(processed_sections)), "inline": True},
            {"name": "Total Sections", "value": str(len(sections)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0x1ABC9C)

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
        log.error("Visuals step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "visuals", exc)
        raise

if __name__ == "__main__":
    import sys
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
    sys.exit(0)