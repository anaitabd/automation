import json
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
import boto3
from botocore.exceptions import ClientError
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

try:
    import nova_canvas
    import nova_reel
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
    import nova_canvas
    import nova_reel

log = get_logger("nexus-visuals")

# Caps concurrent Bedrock API calls to 4 regardless of thread count
bedrock_semaphore = threading.Semaphore(4)

_cache: dict = {}

rekognition = boto3.client("rekognition")
bedrock = boto3.client("bedrock-runtime")
s3_client = boto3.client("s3")


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def invoke_with_backoff(fn, payload: dict, max_retries: int = 5) -> dict:
    """Invoke a Bedrock API callable with exponential backoff + jitter on ThrottlingException."""
    for attempt in range(max_retries):
        try:
            return fn(**payload)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ThrottlingException" and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                log.warning(
                    "Bedrock ThrottlingException (attempt %d/%d) — retrying in %.2fs",
                    attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise


S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")

SCRATCH_DIR = os.environ.get("TMPDIR", "/mnt/scratch")

NOVA_REEL_DURATION_SEC = int(os.environ.get("NOVA_REEL_DURATION_SEC", "6"))
NOVA_REEL_FPS = int(os.environ.get("NOVA_REEL_FPS", "24"))
NOVA_REEL_WIDTH = int(os.environ.get("NOVA_REEL_WIDTH", "1280"))
NOVA_REEL_HEIGHT = int(os.environ.get("NOVA_REEL_HEIGHT", "720"))


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


def _detect_image_format(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "jpeg"


_TRUE_CRIME_BOOST_LABELS = {"person", "night", "building", "road", "vehicle", "darkness", "shadow", "forest"}
_TRUE_CRIME_PENALIZE_LABELS = {"beach", "flowers", "sunshine", "party", "food"}


def _rekognition_score(image_bytes: bytes, visual_cue: str, profile: dict | None = None) -> float:
    response = rekognition.detect_labels(Image={"Bytes": image_bytes}, MaxLabels=20, MinConfidence=50)
    label_names = {l["Name"].lower() for l in response["Labels"]}
    cue_words = set(visual_cue.lower().split())
    base = len(label_names & cue_words) / max(len(cue_words), 1)
    if profile and profile.get("script", {}).get("style") == "true_crime":
        boost = 0.1 * len(label_names & _TRUE_CRIME_BOOST_LABELS)
        penalty = 0.15 * len(label_names & _TRUE_CRIME_PENALIZE_LABELS)
        base = min(1.0, max(0.0, base + boost - penalty))
    return base


def _claude_vision_score(image_bytes: bytes, visual_cue: str) -> float:
    fmt = _detect_image_format(image_bytes)
    payload = {
        "modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "messages": [{"role": "user", "content": [
            {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
            {"text": f"Score 0.0 to 1.0 how well this image matches: '{visual_cue}'. Reply with only the number."}
        ]}],
    }
    with bedrock_semaphore:
        response = invoke_with_backoff(bedrock.converse, payload)
    return float(response["output"]["message"]["content"][0]["text"].strip())


def _select_best_candidate(candidates: list[tuple[bytes, Any]], visual_cue: str,
                           profile: dict | None = None) -> bytes | None:
    if not candidates:
        return None
    scored = []
    for image_bytes, identifier in candidates:
        try:
            score = _rekognition_score(image_bytes, visual_cue, profile)
        except Exception:
            score = 0.0
        scored.append((score, image_bytes, identifier))
    scored.sort(key=lambda x: x[0], reverse=True)
    top3 = scored[:3]
    best_score = -1.0
    best_bytes = top3[0][1]
    for _, image_bytes, identifier in top3:
        try:
            score = _claude_vision_score(image_bytes, visual_cue)
        except Exception:
            score = 0.0
        if score > best_score:
            best_score = score
            best_bytes = image_bytes
    return best_bytes


def _fetch_pexels_video(query: str, min_duration: int = 5, tmpdir: str = "/tmp",
                        scene_id: int = 0) -> str | None:
    """Fetch a landscape video from Pexels. Returns local path or None."""
    try:
        secret = get_secret("nexus/pexels_api_key")
        api_key = secret.get("api_key", "")
        if not api_key:
            return None
    except Exception:
        return None

    encoded_q = urllib.parse.quote(query)
    url = (
        f"https://api.pexels.com/videos/search?query={encoded_q}"
        f"&orientation=landscape&size=large&per_page=10"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": api_key,
        "User-Agent": "NexusCloud/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        log.warning("Scene %d: Pexels video search failed: %s", scene_id, exc)
        return None

    videos = data.get("videos", [])
    for video in videos:
        if video.get("duration", 0) < min_duration:
            continue
        files = video.get("video_files", [])
        for vf in sorted(files, key=lambda f: f.get("height", 0) * f.get("width", 0), reverse=True):
            dl_url = vf.get("link", "")
            if not dl_url:
                continue
            if vf.get("quality", "").lower() not in ("hd", "fhd", "uhd", ""):
                continue
            local_path = os.path.join(tmpdir, f"pexels_vid_{scene_id}.mp4")
            try:
                urllib.request.urlretrieve(dl_url, local_path)
                log.info("Scene %d: Pexels video downloaded: %s", scene_id, local_path)
                return local_path
            except Exception:
                continue
    return None


def _fetch_pexels_photo(query: str, tmpdir: str = "/tmp", scene_id: int = 0) -> bytes | None:
    """Fetch a landscape photo from Pexels. Returns image bytes or None."""
    try:
        secret = get_secret("nexus/pexels_api_key")
        api_key = secret.get("api_key", "")
        if not api_key:
            return None
    except Exception:
        return None

    encoded_q = urllib.parse.quote(query)
    url = (
        f"https://api.pexels.com/v1/search?query={encoded_q}"
        f"&orientation=landscape&size=large&per_page=5"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": api_key,
        "User-Agent": "NexusCloud/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        log.warning("Scene %d: Pexels photo search failed: %s", scene_id, exc)
        return None

    photos = data.get("photos", [])
    for photo in photos:
        src = photo.get("src", {})
        photo_url = src.get("large2x", src.get("large", src.get("original", "")))
        if not photo_url:
            continue
        try:
            with urllib.request.urlopen(urllib.request.Request(
                photo_url, headers={"User-Agent": "NexusCloud/1.0"}
            ), timeout=30) as img_resp:
                return img_resp.read()
        except Exception:
            continue
    return None


def _generate_dark_gradient_video(duration: int, width: int, height: int,
                                   tmpdir: str, scene_id: int) -> str:
    """Generate a dark gradient fallback video using FFmpeg."""
    import subprocess
    out_path = os.path.join(tmpdir, f"gradient_{scene_id}.mp4")
    filter_expr = (
        f"gradients=s={width}x{height}:nb_colors=4:c0=0x0a0a0a:c1=0x1a1a2e"
        f":c2=0x16213e:c3=0x0f3460:duration={duration}"
    )
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", filter_expr,
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", "-t", str(duration),
             out_path],
            check=True, capture_output=True, timeout=60,
        )
        return out_path
    except Exception:
        pass
    # Ultra-safe fallback: solid dark color
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c=0x0a0a0a:s={width}x{height}:d={duration}",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
             "-pix_fmt", "yuv420p", out_path],
            check=True, capture_output=True, timeout=60,
        )
    except Exception as exc:
        log.error("Scene %d: dark gradient fallback failed: %s", scene_id, exc)
    return out_path


def _process_scene(
    scene: dict,
    run_id: str,
    color_grade: str,
    profile: dict | None = None,
) -> dict:
    scene_id = scene.get("scene_id", 0)
    canvas_prompt = scene.get("nova_canvas_prompt", "")
    reel_prompt = scene.get("nova_reel_prompt", "")
    visual_cue = scene.get("visual_cue", canvas_prompt)
    if isinstance(visual_cue, dict):
        visual_cue = canvas_prompt
    estimated_duration = int(scene.get("estimated_duration", NOVA_REEL_DURATION_SEC))
    duration_seconds = min(max(estimated_duration, NOVA_REEL_DURATION_SEC), 60)

    visuals_cfg = (profile or {}).get("visuals", {})
    avoid_nova_reel: bool = visuals_cfg.get("avoid_nova_reel", False)
    pexels_profile_keywords: list[str] = visuals_cfg.get("pexels_keywords", [])

    scene_description = scene.get("description", canvas_prompt)
    scene_keywords = " ".join(scene_description.split()[:6]) if scene_description else visual_cue

    tmpdir = os.path.join(SCRATCH_DIR, run_id, f"scene_{scene_id:03d}")
    os.makedirs(tmpdir, exist_ok=True)

    image_s3_key = f"{run_id}/images/scene_{scene_id:03d}.png"
    candidates: list[tuple[bytes, str]] = scene.get("image_candidates", [])

    clip_type = "video"

    # ── Tier 1: Pexels video ─────────────────────────────────────────────────
    pexels_clip_local: str | None = None
    if pexels_profile_keywords:
        for kw in pexels_profile_keywords[:3]:
            query = f"{scene_keywords} {kw}"
            pexels_clip_local = _fetch_pexels_video(
                query, min_duration=5, tmpdir=tmpdir, scene_id=scene_id
            )
            if pexels_clip_local:
                log.info("Scene %d: Pexels video found (query=%r)", scene_id, query)
                break
    else:
        pexels_clip_local = _fetch_pexels_video(
            visual_cue, min_duration=5, tmpdir=tmpdir, scene_id=scene_id
        )

    if pexels_clip_local:
        clip_s3_key = f"{run_id}/clips/scene_{scene_id:03d}.mp4"
        with open(pexels_clip_local, "rb") as f:
            s3_client.put_object(Bucket=S3_OUTPUTS_BUCKET, Key=clip_s3_key, Body=f.read())
        log.info("Scene %d: Pexels video clip uploaded to %s", scene_id, clip_s3_key)
        return {
            **scene,
            "image_s3_key": image_s3_key,
            "clip_s3_key": clip_s3_key,
            "clip_type": "video",
            "color_grade": color_grade,
        }

    # ── Tier 2: Pexels photo + Ken Burns flag ────────────────────────────────
    pexels_query = f"{scene_keywords} {pexels_profile_keywords[0]}" if pexels_profile_keywords else visual_cue
    pexels_photo_bytes = _fetch_pexels_photo(pexels_query, tmpdir=tmpdir, scene_id=scene_id)
    if pexels_photo_bytes:
        log.info("Scene %d: Pexels photo found — flagging as static_image", scene_id)
        s3_client.put_object(Bucket=S3_OUTPUTS_BUCKET, Key=image_s3_key, Body=pexels_photo_bytes)
        clip_s3_key = f"{run_id}/clips/scene_{scene_id:03d}.mp4"
        # Placeholder — editor will apply Ken Burns; store the image key as the clip key
        s3_client.put_object(Bucket=S3_OUTPUTS_BUCKET, Key=clip_s3_key, Body=pexels_photo_bytes)
        return {
            **scene,
            "image_s3_key": image_s3_key,
            "clip_s3_key": clip_s3_key,
            "clip_type": "static_image",
            "color_grade": color_grade,
        }

    # ── Tier 3: Nova Canvas dark atmospheric image + Ken Burns flag ──────────
    log.info("Scene %d: No Pexels result — generating Nova Canvas image", scene_id)
    if candidates:
        log.info("Scene %d: scoring %d image candidates with Rekognition + Claude", scene_id, len(candidates))
        best_image_bytes = _select_best_candidate(candidates, visual_cue, profile)
        if best_image_bytes is not None:
            s3_client.put_object(Bucket=S3_OUTPUTS_BUCKET, Key=image_s3_key, Body=best_image_bytes)
            log.info("Scene %d: best candidate uploaded to %s", scene_id, image_s3_key)
        else:
            log.warning("Scene %d: candidate selection returned None", scene_id)
            candidates = []

    if not candidates:
        is_true_crime = (profile or {}).get("script", {}).get("style") == "true_crime"
        if is_true_crime:
            _canvas_prompt = (
                canvas_prompt.strip()
                or f"Dark atmospheric cinematic shot, dramatic shadows, moody lighting, scene {scene_id}"
            )
            dark_suffix = ", deep shadows, dark atmosphere, moody, photorealistic, no text"
            _canvas_prompt = f"{_canvas_prompt}{dark_suffix}"
        else:
            _canvas_prompt = (
                canvas_prompt.strip()
                or f"Cinematic wide shot, documentary style, scene {scene_id}, dramatic lighting"
            )
        try:
            with bedrock_semaphore:
                nova_canvas.generate_and_upload_image(
                    prompt=_canvas_prompt,
                    s3_key=image_s3_key,
                    bucket=S3_OUTPUTS_BUCKET,
                    width=NOVA_REEL_WIDTH,
                    height=NOVA_REEL_HEIGHT,
                )
        except Exception as canvas_exc:
            if "content filters" in str(canvas_exc).lower() or "blocked" in str(canvas_exc).lower():
                log.warning("Scene %d: Nova Canvas content filter — retrying with neutral prompt", scene_id)
                _neutral = f"Cinematic wide establishing shot, documentary style, dramatic lighting, scene {scene_id}"
                with bedrock_semaphore:
                    nova_canvas.generate_and_upload_image(
                        prompt=_neutral,
                        s3_key=image_s3_key,
                        bucket=S3_OUTPUTS_BUCKET,
                        width=NOVA_REEL_WIDTH,
                        height=NOVA_REEL_HEIGHT,
                    )
            else:
                raise
        log.info("Scene %d: Nova Canvas image uploaded to %s", scene_id, image_s3_key)

    # Nova Canvas image uploaded — treat as static_image for Ken Burns in editor
    # But also upload a copy as the clip key so the editor can find it
    try:
        image_obj = s3_client.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=image_s3_key)
        image_bytes_for_clip = image_obj["Body"].read()
    except Exception:
        image_bytes_for_clip = b""

    clip_s3_key = f"{run_id}/clips/scene_{scene_id:03d}.mp4"
    if image_bytes_for_clip:
        s3_client.put_object(Bucket=S3_OUTPUTS_BUCKET, Key=clip_s3_key, Body=image_bytes_for_clip)

    # ── Tier 4: Nova Reel (only if avoid_nova_reel is False) ─────────────────
    if not avoid_nova_reel:
        image_s3_uri = f"s3://{S3_OUTPUTS_BUCKET}/{image_s3_key}"
        log.info("Scene %d: generating video clip with Nova Reel", scene_id)
        clip_s3_key_reel = f"{run_id}/clips/scene_{scene_id:03d}"
        try:
            with bedrock_semaphore:
                final_clip_key = nova_reel.generate_and_upload_video(
                    text_prompt=reel_prompt,
                    output_s3_key=clip_s3_key_reel,
                    output_s3_bucket=S3_OUTPUTS_BUCKET,
                    image_s3_uri=image_s3_uri,
                    duration_seconds=duration_seconds,
                    fps=NOVA_REEL_FPS,
                    width=NOVA_REEL_WIDTH,
                    height=NOVA_REEL_HEIGHT,
                    seed=scene_id,
                )
            log.info("Scene %d: Nova Reel clip uploaded to s3://%s/%s",
                     scene_id, S3_OUTPUTS_BUCKET, final_clip_key)
            return {
                **scene,
                "image_s3_key": image_s3_key,
                "clip_s3_key": final_clip_key,
                "clip_type": "video",
                "color_grade": color_grade,
            }
        except Exception as reel_exc:
            log.warning("Scene %d: Nova Reel failed (%s) — falling back to static_image", scene_id, reel_exc)

    # ── Tier 5: Static image fallback (Ken Burns in editor) ──────────────────
    log.info("Scene %d: using static_image (Ken Burns will be applied in editor)", scene_id)
    return {
        **scene,
        "image_s3_key": image_s3_key,
        "clip_s3_key": clip_s3_key,
        "clip_type": "static_image",
        "color_grade": color_grade,
    }


def lambda_handler(event: dict, context) -> dict:
    handler_start = time.time()
    deadline = handler_start + 7140

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
        color_grade = profile.get("visuals", {}).get("color_grade_default", "cinematic_warm")

        scenes: list[dict] = script.get("scenes", [])
        log.info("Visuals pipeline starting — %d scenes, profile=%s, dry_run=%s",
                 len(scenes), profile_name, dry_run)

        if dry_run:
            processed_scenes = [
                {
                    **scene,
                    "image_s3_key": f"{run_id}/images/scene_{scene.get('scene_id', i):03d}_dry_run.png",
                    "clip_s3_key": f"{run_id}/clips/scene_{scene.get('scene_id', i):03d}_dry_run.mp4",
                    "clip_type": "video",
                    "color_grade": color_grade,
                }
                for i, scene in enumerate(scenes)
            ]
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "mixed_audio_s3_key": mixed_audio_s3_key,
                "total_duration_estimate": total_duration_estimate,
                "title": script.get("title", event.get("title", "")),
                "scenes": processed_scenes,
            }

        max_workers = min(len(scenes), int(os.environ.get("VISUALS_PARALLELISM", 2)))
        processed_scenes: list[dict] = []

        def _process_one(scene: dict) -> tuple[dict | None, float]:
            scene_start = time.time()
            log.info("━━ Processing scene %s ━━", scene.get("scene_id"))
            try:
                result = _process_scene(scene, run_id, color_grade, profile)
            except Exception as exc:
                log.error("Scene %s failed: %s", scene.get("scene_id"), exc, exc_info=True)
                result = None
            elapsed = time.time() - scene_start
            log.info("Scene %s done in %.1fs (success=%s)", scene.get("scene_id"), elapsed, result is not None)
            return result, elapsed

        log.info("Processing %d scenes with %d parallel workers (deadline in %.0fs)",
                 len(scenes), max_workers, deadline - time.time())
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, scene in enumerate(scenes):
                time.sleep(i * 0.5)  # stagger submissions to avoid burst at t=0
                futures[executor.submit(_process_one, scene)] = scene
            remaining = max(10, deadline - time.time())
            try:
                for future in as_completed(futures, timeout=remaining):
                    result, elapsed = future.result()
                    if result:
                        processed_scenes.append(result)
            except TimeoutError:
                done_count = sum(1 for f in futures if f.done())
                log.warning("Deadline approaching — returning %d/%d scenes (completed %d futures)",
                            len(processed_scenes), len(scenes), done_count)
                for f in futures:
                    f.cancel()

        processed_scenes.sort(key=lambda s: s.get("scene_id", 0))

        script["scenes"] = processed_scenes
        updated_script_key = f"{run_id}/script_with_assets.json"
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=updated_script_key,
            Body=json.dumps(script, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        log.info("Updated EDL written to s3://%s/%s", S3_OUTPUTS_BUCKET, updated_script_key)

        log.info("Visuals complete — %d/%d scenes produced clips", len(processed_scenes), len(scenes))

        elapsed = time.time() - step_start
        notify_step_complete("visuals", run_id, [
            {"name": "Clips Processed", "value": str(len(processed_scenes)), "inline": True},
            {"name": "Total Scenes", "value": str(len(scenes)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0x1ABC9C)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "script_s3_key": script_s3_key,
            "edl_s3_key": updated_script_key,
            "mixed_audio_s3_key": mixed_audio_s3_key,
            "total_duration_estimate": total_duration_estimate,
            "scenes": processed_scenes,
            "title": script.get("title", ""),
            "mood": script.get("mood", ""),
        }

    except Exception as exc:
        log.error("Visuals step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "visuals", exc)
        raise
    finally:
        import shutil
        scratch_dir = os.path.join(SCRATCH_DIR, run_id)
        shutil.rmtree(scratch_dir, ignore_errors=True)

if __name__ == "__main__":
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
    sys.exit(0)
