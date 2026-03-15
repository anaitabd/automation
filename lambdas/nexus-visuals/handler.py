import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
import boto3
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

try:
    import nova_canvas
    import nova_reel
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
    import nova_canvas
    import nova_reel

log = get_logger("nexus-visuals")

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


def _rekognition_score(image_bytes: bytes, visual_cue: str) -> float:
    response = rekognition.detect_labels(Image={"Bytes": image_bytes}, MaxLabels=20, MinConfidence=50)
    labels = {l["Name"].lower() for l in response["Labels"]}
    cue_words = set(visual_cue.lower().split())
    return len(labels & cue_words) / max(len(cue_words), 1)


def _claude_vision_score(image_bytes: bytes, visual_cue: str) -> float:
    fmt = _detect_image_format(image_bytes)
    response = bedrock.converse(
        modelId="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        messages=[{"role": "user", "content": [
            {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
            {"text": f"Score 0.0 to 1.0 how well this image matches: '{visual_cue}'. Reply with only the number."}
        ]}]
    )
    return float(response["output"]["message"]["content"][0]["text"].strip())


def _select_best_candidate(candidates: list[tuple[bytes, Any]], visual_cue: str) -> bytes | None:
    if not candidates:
        return None
    scored = []
    for image_bytes, identifier in candidates:
        try:
            score = _rekognition_score(image_bytes, visual_cue)
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


def _process_scene(
    scene: dict,
    run_id: str,
    color_grade: str,
) -> dict:
    scene_id = scene.get("scene_id", 0)
    canvas_prompt = scene.get("nova_canvas_prompt", "")
    reel_prompt = scene.get("nova_reel_prompt", "")
    visual_cue = scene.get("visual_cue", canvas_prompt)
    if isinstance(visual_cue, dict):
        visual_cue = canvas_prompt
    estimated_duration = int(scene.get("estimated_duration", NOVA_REEL_DURATION_SEC))
    duration_seconds = min(max(estimated_duration, NOVA_REEL_DURATION_SEC), 60)

    image_s3_key = f"{run_id}/images/scene_{scene_id:03d}.png"
    candidates: list[tuple[bytes, str]] = scene.get("image_candidates", [])

    if candidates:
        log.info("Scene %d: scoring %d image candidates with Rekognition + Claude", scene_id, len(candidates))
        best_image_bytes = _select_best_candidate(candidates, visual_cue)
        if best_image_bytes is not None:
            s3_client.put_object(Bucket=S3_OUTPUTS_BUCKET, Key=image_s3_key, Body=best_image_bytes)
            log.info("Scene %d: best candidate uploaded to %s", scene_id, image_s3_key)
        else:
            log.warning("Scene %d: candidate selection returned None, falling back to Nova Canvas", scene_id)
            candidates = []

    if not candidates:
        log.info("Scene %d: generating base image with Nova Canvas", scene_id)
        nova_canvas.generate_and_upload_image(
            prompt=canvas_prompt,
            s3_key=image_s3_key,
            bucket=S3_OUTPUTS_BUCKET,
            width=NOVA_REEL_WIDTH,
            height=NOVA_REEL_HEIGHT,
        )
        log.info("Scene %d: base image uploaded to %s", scene_id, image_s3_key)

    image_s3_uri = f"s3://{S3_OUTPUTS_BUCKET}/{image_s3_key}"

    log.info("Scene %d: generating video clip with Nova Reel", scene_id)
    clip_s3_key = f"{run_id}/clips/scene_{scene_id:03d}"
    final_clip_key = nova_reel.generate_and_upload_video(
        text_prompt=reel_prompt,
        output_s3_key=clip_s3_key,
        output_s3_bucket=S3_OUTPUTS_BUCKET,
        image_s3_uri=image_s3_uri,
        duration_seconds=duration_seconds,
        fps=NOVA_REEL_FPS,
        width=NOVA_REEL_WIDTH,
        height=NOVA_REEL_HEIGHT,
        seed=scene_id,
    )
    log.info("Scene %d: clip uploaded to s3://%s/%s", scene_id, S3_OUTPUTS_BUCKET, final_clip_key)

    return {
        **scene,
        "image_s3_key": image_s3_key,
        "clip_s3_key": final_clip_key,
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
                result = _process_scene(scene, run_id, color_grade)
            except Exception as exc:
                log.error("Scene %s failed: %s", scene.get("scene_id"), exc, exc_info=True)
                result = None
            elapsed = time.time() - scene_start
            log.info("Scene %s done in %.1fs (success=%s)", scene.get("scene_id"), elapsed, result is not None)
            return result, elapsed

        log.info("Processing %d scenes with %d parallel workers (deadline in %.0fs)",
                 len(scenes), max_workers, deadline - time.time())
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_one, scene): scene for scene in scenes}
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
