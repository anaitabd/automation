import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import boto3

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("nexus-visuals")

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


def _search_pexels(query: str, api_key: str, per_page: int = 8) -> list[str]:
    log.info("Pexels search: query=%r per_page=%d", query, per_page)
    encoded = urllib.parse.quote(query)
    url = f"https://api.pexels.com/videos/search?query={encoded}&per_page={per_page}&orientation=landscape&size=large"
    headers = {"Authorization": api_key}
    try:
        data = json.loads(_http_get(url, headers=headers))
        urls = []
        for video in data.get("videos", []):
            # Prefer HD files with width >= 1920 first, then fall back to 1280+
            best_file = None
            for vf in video.get("video_files", []):
                w = vf.get("width", 0)
                q = vf.get("quality", "")
                if q == "hd" and w >= 1920:
                    best_file = vf["link"]
                    break
                elif q in ("hd", "sd") and w >= 1280 and not best_file:
                    best_file = vf["link"]
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
                urls.append(f"https://archive.org/download/{ident}/{ident}.mp4")
        log.info("Archive.org returned %d candidates", len(urls))
        return urls
    except Exception as exc:
        log.warning("Archive.org search failed: %s", exc)
        return []


def _generate_runway(prompt: str, api_key: str, tmpdir: str) -> str | None:
    log.info("Runway gen4_turbo: prompt=%r", prompt[:80])
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Runway-Version": "2024-11-06",
    }
    try:
        body = {
            "model": "gen4_turbo",
            "promptText": prompt,
            "duration": 10,
            "ratio": "1920:1080",
        }
        response = _http_post("https://api.dev.runwayml.com/v1/image_to_video", headers=headers, body=body)
        task_id = response.get("id")
        if not task_id:
            log.warning("Runway returned no task_id")
            return None

        log.info("Runway task %s — polling (up to 180s)…", task_id)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(5)
            poll_url = f"https://api.dev.runwayml.com/v1/tasks/{task_id}"
            poll_headers = {"User-Agent": "NexusCloud/1.0"}
            poll_headers.update(headers)
            req = urllib.request.Request(poll_url, headers=poll_headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                task = json.loads(resp.read())
            status = task.get("status")
            if status == "SUCCEEDED":
                video_url = task.get("output", [None])[0]
                if video_url:
                    log.info("Runway task %s SUCCEEDED — downloading", task_id)
                    video_bytes = _http_get(video_url)
                    out_path = os.path.join(tmpdir, f"runway_{task_id}.mp4")
                    with open(out_path, "wb") as f:
                        f.write(video_bytes)
                    return out_path
                break
            elif status in ("FAILED", "CANCELLED"):
                log.warning("Runway task %s ended with status=%s", task_id, status)
                break

        log.warning("Runway task %s timed out or had no output", task_id)
        return None
    except Exception as exc:
        log.warning("Runway generation failed: %s", exc)
        return None


def _download_video(url: str, tmpdir: str, idx: int | str) -> str | None:
    try:
        log.info("Downloading video %s …", idx)
        video_bytes = _http_get(url)
        path = os.path.join(tmpdir, f"raw_{idx}.mp4")
        with open(path, "wb") as f:
            f.write(video_bytes)
        log.info("Downloaded video %s (%.1f MB)", idx, len(video_bytes) / 1_048_576)
        return path
    except Exception as exc:
        log.warning("Download failed for video %s: %s", idx, exc)
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
            "zoompan=z='min(zoom+0.0005,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            ":d=150:s=1920x1080:fps=30"
        )
    elif style == "ken_burns_out":
        return (
            "zoompan=z='if(lte(zoom,1.0),1.06,max(1.001,zoom-0.0005))'"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=150:s=1920x1080:fps=30"
        )
    elif style == "pan_left":
        return (
            "zoompan=z='1.06':x='iw/2-(iw/zoom/2)+t*2':y='ih/2-(ih/zoom/2)'"
            ":d=150:s=1920x1080:fps=30"
        )
    elif style == "pan_right":
        return (
            "zoompan=z='1.06':x='iw/2-(iw/zoom/2)-t*2':y='ih/2-(ih/zoom/2)'"
            ":d=150:s=1920x1080:fps=30"
        )
    elif style == "slow_drift":
        # Subtle diagonal drift — very cinematic
        return (
            "zoompan=z='1.04':x='iw/2-(iw/zoom/2)+t*1.5':y='ih/2-(ih/zoom/2)+t*0.8'"
            ":d=150:s=1920x1080:fps=30"
        )
    elif style == "dolly_in":
        # Faster zoom simulating a dolly push-in
        return (
            "zoompan=z='min(zoom+0.001,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            ":d=150:s=1920x1080:fps=30"
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
    target_duration: float | None = None,
) -> bool:
    try:
        raw_dur = _get_duration(raw_path)
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

        filters.append(_build_camera_motion_filter(camera_style))

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
    has_runway: bool,
    pexels_key: str,
    runway_key: str,
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

    # ── Gather candidates from all queries (use up to 4 queries, 8 results each) ──
    candidates: list[str] = []
    for query in queries[:4]:
        candidates += _search_pexels(query, pexels_key, per_page=8)
        if "archive_org" in archive_enabled:
            candidates += _search_archive_org(query, per_page=4)

    # Deduplicate URLs
    seen_urls: set[str] = set()
    unique_candidates: list[str] = []
    for url in candidates:
        if url not in seen_urls:
            seen_urls.add(url)
            unique_candidates.append(url)
    candidates = unique_candidates

    log.info("Section %d: %d unique candidates to score (max 15)", section_idx, len(candidates))

    # ── Score and rank all candidates ──
    scored: list[tuple[float, str]] = []
    query_text = " ".join(queries)
    for idx, url in enumerate(candidates[:15]):
        raw_path = _download_video(url, tmpdir, f"{section_idx}_{idx}")
        if raw_path is None:
            continue
        score = _score_clip(raw_path, query_text)
        scored.append((score, raw_path))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)
    log.info("Section %d: %d clips scored, best=%.2f, threshold=%.2f",
             section_idx, len(scored), scored[0][0] if scored else 0, threshold)

    # ── Fill with Runway-generated clips if not enough good candidates ──
    good_clips = [(s, p) for s, p in scored if s >= threshold]
    if len(good_clips) < clips_needed and has_runway:
        for rw_idx, query in enumerate(queries[:2]):
            if len(good_clips) >= clips_needed:
                break
            runway_path = _generate_runway(query, runway_key, tmpdir)
            if runway_path:
                runway_score = _score_clip(runway_path, query_text)
                good_clips.append((runway_score, runway_path))
                scored.append((runway_score, runway_path))
                scored.sort(key=lambda x: x[0], reverse=True)

    # Take the top N clips
    selected = scored[:clips_needed] if scored else []

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
                "title": f"🎬 Nexus Cloud — {step}",
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
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    mixed_audio_s3_key: str = event.get("mixed_audio_s3_key", "")
    total_duration_estimate: float = float(event.get("total_duration_estimate", 0))

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
                log.info("━━ Processing section %d/%d ━━", idx + 1, len(sections))
                section_start = time.time()
                result = _source_and_process_section(
                    section=section,
                    section_idx=idx,
                    profile=profile,
                    profile_name=profile_name,
                    lut_local_path=lut_local_path,
                    tmpdir=tmpdir,
                    s3=s3,
                    run_id=run_id,
                    has_runway=has_runway,
                    pexels_key=pexels_key,
                    runway_key=runway_key,
                )
                if result:
                    processed_sections.append(result)
                log.info("Section %d/%d done in %.1fs (success=%s)",
                         idx + 1, len(sections), time.time() - section_start, result is not None)

        log.info("Visuals complete — %d/%d sections produced clips", len(processed_sections), len(sections))

        _notify_discord("Visuals Sourced", 0x1ABC9C, run_id, [
            {"name": "Clips Processed", "value": str(len(processed_sections)), "inline": True},
            {"name": "Total Sections", "value": str(len(sections)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], dry_run=dry_run)

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
        log.error("Visuals failed: %s", exc, exc_info=True)
        _write_error(run_id, "visuals", exc)
        raise
