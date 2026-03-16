import json
import logging
import os
import time
import urllib.request
import uuid
from datetime import datetime

import boto3

import db
import preflight

log = logging.getLogger("nexus-api")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
ECS_SUBNETS = json.loads(os.environ.get("ECS_SUBNETS", "[]"))
REQUIRE_API_KEY = os.environ.get("REQUIRE_API_KEY", "false").lower() == "true"
CHANNEL_SETUP_FUNCTION = os.environ.get("CHANNEL_SETUP_FUNCTION", "nexus-channel-setup")

sfn = boto3.client("stepfunctions")
s3 = boto3.client("s3")


def _enrich_channel(channel: dict) -> dict:
    """Add presigned URLs (logo_url, intro_url, outro_url) from ASSETS_BUCKET to a channel dict."""
    brand = channel.get("brand") or {}
    if isinstance(brand, str):
        try:
            brand = json.loads(brand)
        except Exception:
            brand = {}
    for s3_field, url_field in (("logo_s3", "logo_url"), ("intro_s3", "intro_url"), ("outro_s3", "outro_url")):
        key = brand.get(s3_field, "")
        if key:
            try:
                brand[url_field] = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": ASSETS_BUCKET, "Key": key},
                    ExpiresIn=3600,
                )
            except Exception:
                brand[url_field] = ""
        else:
            brand[url_field] = ""
    channel["brand"] = brand
    return channel


PIPELINE_STEPS = ["Research", "Script", "Audio", "Visuals", "Editor", "Shorts", "Thumbnail", "Upload", "Notify"]
VALID_SHORTS_TIERS = {"micro", "short", "mid", "full"}
_PARALLEL_CONTAINER_STATES = frozenset(["AudioVisuals", "ContentAssembly"])
_TRACKABLE_STATES = frozenset(PIPELINE_STEPS) | _PARALLEL_CONTAINER_STATES
_SKIP_STATE_NAMES = frozenset([
    "NotifyError", "PipelineFailed", "MergeParallelOutputs",
    "MergeContentOutputs", "SetAudioKeys", "SetEditorKeys",
    "SetShortsKeys", "ShortsSkipped", "CheckShortsEnabled",
    "ResearchError", "ScriptError", "AudioVisualsError",
    "EditorError", "ShortsError", "ThumbnailError", "UploadError",
])


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,x-api-key",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _check_api_key(event: dict) -> bool:
    if not REQUIRE_API_KEY:
        return True
    headers = event.get("headers") or {}
    key = headers.get("x-api-key") or headers.get("X-Api-Key") or ""
    return bool(key)


def _execution_arn(run_id: str) -> str:
    return f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:{run_id}"


def _build_step_history(execution_arn: str) -> list[dict]:
    events = []
    next_token = None
    while True:
        kwargs = {"executionArn": execution_arn, "maxResults": 100, "reverseOrder": False}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = sfn.get_execution_history(**kwargs)
        events.extend(resp.get("events", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break

    step_data = {}
    running_steps = set()

    for ev in events:
        etype = ev.get("type", "")
        ts = ev.get("timestamp")
        ts_str = ts.isoformat() if ts else None

        if etype in ("TaskStateEntered", "ParallelStateEntered"):
            name = ev.get("stateEnteredEventDetails", {}).get("name", "")
            if name and name not in _SKIP_STATE_NAMES:
                if name in _TRACKABLE_STATES:
                    step_data[name] = {
                        "name": name,
                        "status": "running",
                        "entered_at": ts_str,
                        "exited_at": None,
                        "duration_sec": None,
                        "error": None,
                        "parallel": name in ("Audio", "Visuals", "AudioVisuals", "Editor", "Shorts", "ContentAssembly"),
                    }
                    running_steps.add(name)

        elif etype in ("TaskStateExited", "ParallelStateExited"):
            name = ev.get("stateExitedEventDetails", {}).get("name", "")
            if name in step_data and step_data[name]["status"] == "running":
                step_data[name]["status"] = "done"
                step_data[name]["exited_at"] = ts_str
                if step_data[name]["entered_at"] and ts:
                    try:
                        entered = datetime.fromisoformat(step_data[name]["entered_at"])
                        step_data[name]["duration_sec"] = round((ts - entered).total_seconds(), 1)
                    except Exception:
                        pass
                running_steps.discard(name)

        elif etype == "TaskFailed":
            details = ev.get("taskFailedEventDetails", {})
            error_type = details.get("error", "Unknown")
            cause_raw = details.get("cause", "")
            error_msg = cause_raw
            stack_trace = ""
            try:
                cause_json = json.loads(cause_raw)
                error_msg = cause_json.get("errorMessage", cause_raw)
                error_type = cause_json.get("errorType", error_type)
                stack_trace = "\n".join(cause_json.get("stackTrace", []))
            except (json.JSONDecodeError, TypeError):
                pass

            for step_name in list(running_steps):
                if step_name in step_data:
                    step_data[step_name]["status"] = "error"
                    step_data[step_name]["exited_at"] = ts_str
                    step_data[step_name]["error"] = {
                        "type": error_type,
                        "message": error_msg[:500],
                        "stack_trace": stack_trace[:1000] if stack_trace else None,
                    }
                    if step_data[step_name]["entered_at"] and ts:
                        try:
                            entered = datetime.fromisoformat(step_data[step_name]["entered_at"])
                            step_data[step_name]["duration_sec"] = round((ts - entered).total_seconds(), 1)
                        except Exception:
                            pass

    steps = []
    for step_name in PIPELINE_STEPS:
        if step_name in step_data:
            steps.append(step_data[step_name])
        else:
            steps.append({
                "name": step_name,
                "status": "pending",
                "entered_at": None,
                "exited_at": None,
                "duration_sec": None,
                "error": None,
                "parallel": step_name in ("Audio", "Visuals", "Editor", "Shorts"),
            })

    return steps


def _validate_run_body(body: dict) -> str | None:
    niche = body.get("niche")
    if not niche or not isinstance(niche, str) or not niche.strip():
        return "niche is required and must be a non-empty string"
    if len(niche) > 200:
        return "niche must be 200 characters or fewer"

    profile = body.get("profile", "documentary")
    if profile not in ("documentary", "finance", "entertainment"):
        return "profile must be one of: documentary, finance, entertainment"

    generate_shorts = body.get("generate_shorts", False)
    if not isinstance(generate_shorts, bool):
        return "generate_shorts must be a boolean"

    shorts_tiers = body.get("shorts_tiers", "micro,short,mid,full")
    if shorts_tiers:
        tiers = [t.strip() for t in shorts_tiers.split(",") if t.strip()]
        invalid = [t for t in tiers if t not in VALID_SHORTS_TIERS]
        if invalid:
            return f"shorts_tiers contains invalid values: {invalid}. Allowed: {sorted(VALID_SHORTS_TIERS)}"

    channel_id = body.get("channel_id")
    if channel_id is not None and not isinstance(channel_id, str):
        return "channel_id must be a string"

    return None


def _load_preflight_secrets() -> dict:
    sm = boto3.client("secretsmanager")

    def _fetch(name: str) -> dict:
        try:
            return json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
        except Exception:
            return {}

    return {
        "perplexity": _fetch("nexus/perplexity_api_key"),
        "elevenlabs": _fetch("nexus/elevenlabs_api_key"),
        "pexels": _fetch("nexus/pexels_api_key"),
        "discord": _fetch("nexus/discord_webhook_url"),
    }


def _handle_run(body: dict) -> dict:
    validation_error = _validate_run_body(body)
    if validation_error:
        return _response(400, {"error": validation_error})

    niche = body["niche"].strip()
    profile = body.get("profile", "documentary")
    dry_run = bool(body.get("dry_run", False))
    generate_shorts = bool(body.get("generate_shorts", False))
    shorts_tiers = body.get("shorts_tiers", [])
    channel_id = body.get("channel_id") or None
    pipeline_type = body.get("pipeline_type", "video")

    if not dry_run:
        try:
            secrets = _load_preflight_secrets()
            preflight_result = preflight.run_preflight_checks(secrets)
            if not preflight_result["ok"]:
                return _response(
                    503,
                    {
                        "error": "One or more required external services are unavailable",
                        "preflight": preflight_result["checks"],
                    },
                )
        except Exception as exc:
            log.warning("Preflight check failed, proceeding anyway: %s", exc)

    run_id = str(uuid.uuid4())
    execution = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=run_id,
        input=json.dumps(
            {
                "run_id": run_id,
                "niche": niche,
                "profile": profile,
                "dry_run": dry_run,
                "subnets": ECS_SUBNETS,
                "generate_shorts": generate_shorts,
                "shorts_tiers": shorts_tiers,
                "channel_id": channel_id,
                "pipeline_type": pipeline_type,
            }
        ),
    )
    return _response(
        200,
        {
            "run_id": run_id,
            "execution_arn": execution["executionArn"],
        },
    )


def _handle_status(run_id: str) -> dict:
    try:
        exec_arn = _execution_arn(run_id)
        detail = sfn.describe_execution(executionArn=exec_arn)

        steps = _build_step_history(exec_arn)

        done_count = sum(1 for s in steps if s["status"] == "done")
        error_count = sum(1 for s in steps if s["status"] == "error")
        total = len(PIPELINE_STEPS)
        progress_pct = round(((done_count + error_count) / total) * 100) if total else 0
        if detail["status"] == "SUCCEEDED":
            progress_pct = 100

        # Find current step
        current_step = None
        current_step_index = None
        for i, s in enumerate(steps):
            if s["status"] == "running":
                current_step = s["name"]
                current_step_index = i
                break
            elif s["status"] == "error":
                current_step = s["name"]
                current_step_index = i

        # Top-level error info
        top_error = None
        if detail["status"] == "FAILED":
            top_error = {
                "error": detail.get("error", ""),
                "cause": detail.get("cause", ""),
            }
            # Also get the specific step error
            for s in steps:
                if s["status"] == "error" and s["error"]:
                    top_error["step"] = s["name"]
                    top_error["step_error"] = s["error"]
                    break

        # Build timeline log entries from steps
        timeline = []
        for s in steps:
            if s["entered_at"]:
                timeline.append({
                    "time": s["entered_at"],
                    "step": s["name"],
                    "event": "started",
                    "message": f"{s['name']} started",
                })
            if s["status"] == "done" and s["exited_at"]:
                dur = f" ({s['duration_sec']}s)" if s["duration_sec"] else ""
                timeline.append({
                    "time": s["exited_at"],
                    "step": s["name"],
                    "event": "completed",
                    "message": f"{s['name']} completed{dur}",
                })
            elif s["status"] == "error" and s["error"]:
                timeline.append({
                    "time": s["exited_at"] or s["entered_at"],
                    "step": s["name"],
                    "event": "failed",
                    "message": f"{s['name']} failed: [{s['error']['type']}] {s['error']['message'][:200]}",
                })

        # Compute elapsed
        elapsed_sec = None
        if detail.get("startDate"):
            end = detail.get("stopDate") or time.time()
            if hasattr(end, 'timestamp'):
                end = end.timestamp()
            elapsed_sec = round(end - detail["startDate"].timestamp(), 1)

        return _response(
            200,
            {
                "run_id": run_id,
                "status": detail["status"],
                "current_step": current_step,
                "current_step_index": current_step_index,
                "total_steps": total,
                "progress_pct": progress_pct,
                "start_date": detail["startDate"].isoformat(),
                "stop_date": detail["stopDate"].isoformat() if detail.get("stopDate") else None,
                "elapsed_sec": elapsed_sec,
                "steps": steps,
                "timeline": timeline,
                "error": top_error,
            },
        )
    except sfn.exceptions.ExecutionDoesNotExist:
        return _response(404, {"error": "run not found"})
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _handle_outputs(run_id: str) -> dict:
    output_files = [
        f"{run_id}/final_video.mp4",
        f"{run_id}/final_video_dry_run.mp4",
        f"{run_id}/thumbnails/thumbnail_0.jpg",
        f"{run_id}/thumbnails/thumbnail_1.jpg",
        f"{run_id}/thumbnails/thumbnail_2.jpg",
        f"{run_id}/script.json",
        f"{run_id}/research.json",
    ]

    presigned_urls = {}
    for key in output_files:
        try:
            s3.head_object(Bucket=OUTPUTS_BUCKET, Key=key)
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": OUTPUTS_BUCKET, "Key": key},
                ExpiresIn=3600,
            )
            presigned_urls[key] = url
        except Exception:
            pass

    # Also collect error logs
    error_logs = {}
    try:
        resp = s3.list_objects_v2(Bucket=OUTPUTS_BUCKET, Prefix=f"{run_id}/errors/", MaxKeys=20)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            try:
                error_obj = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=key)
                error_data = json.loads(error_obj["Body"].read())
                step_name = key.split("/")[-1].replace(".json", "")
                error_logs[step_name] = error_data
            except Exception:
                pass
    except Exception:
        pass

    return _response(200, {"run_id": run_id, "urls": presigned_urls, "error_logs": error_logs})


def _handle_resume(body: dict) -> dict:
    run_id = body.get("run_id", "").strip()
    resume_from = body.get("resume_from") or None  # None = auto-detect
    dry_run = bool(body.get("dry_run", False))

    if not run_id:
        return _response(400, {"error": "run_id is required"})

    STEP_ORDER = ["Research", "Script", "AudioVisuals", "Editor", "Thumbnail", "Upload", "Notify"]
    VALID_STEPS = set(STEP_ORDER)
    if resume_from and resume_from not in VALID_STEPS:
        return _response(400, {"error": f"resume_from must be one of {STEP_ORDER}"})

    def s3_exists(bucket, key):
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    # Detect which steps have already produced S3 artifacts
    # research/script/visuals → OUTPUTS_BUCKET, audio → ASSETS_BUCKET
    artifact_checks = [
        ("Research",     OUTPUTS_BUCKET, f"{run_id}/research.json"),
        ("Script",       OUTPUTS_BUCKET, f"{run_id}/script.json"),
        ("AudioVisuals", ASSETS_BUCKET,  f"{run_id}/audio/mixed_audio.wav"),
        ("Editor",       OUTPUTS_BUCKET, f"{run_id}/review/final_video.mp4"),
        ("Thumbnail",    OUTPUTS_BUCKET, f"{run_id}/review/thumbnail_0.jpg"),
    ]
    completed = [step for step, bucket, key in artifact_checks if s3_exists(bucket, key)]

    if resume_from is None:
        for step in STEP_ORDER:
            if step not in completed:
                resume_from = step
                break
        if resume_from is None:
            return _response(400, {"error": "All steps appear complete — nothing to resume"})

    # Read script metadata
    script_meta = {}
    try:
        resp = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=f"{run_id}/script.json")
        sd = json.loads(resp["Body"].read())
        script_meta = {
            "script_s3_key": f"{run_id}/script.json",
            "title": sd.get("title", ""),
            "section_count": sd.get("section_count") or sd.get("scene_count") or len(sd.get("scenes", sd.get("sections", []))),
            "total_duration_estimate": sd.get("total_duration_estimate", 600),
            "niche": sd.get("niche", ""),
            "profile": sd.get("profile", "documentary"),
        }
    except Exception:
        pass

    # Read research metadata
    research_meta = {}
    try:
        resp = s3.get_object(Bucket=OUTPUTS_BUCKET, Key=f"{run_id}/research.json")
        rd = json.loads(resp["Body"].read())
        research_meta = {
            "research_s3_key": f"{run_id}/research.json",
            "selected_topic": rd.get("selected_topic", ""),
            "angle": rd.get("angle", ""),
            "trending_context": rd.get("trending_context", ""),
        }
    except Exception:
        pass

    niche = script_meta.get("niche") or body.get("niche", "")
    profile = script_meta.get("profile") or body.get("profile", "documentary")

    payload = {
        "run_id": run_id,
        "niche": niche,
        "profile": profile,
        "dry_run": dry_run,
        "subnets": ECS_SUBNETS,
        "resume_from": resume_from,
        "generate_shorts": bool(body.get("generate_shorts", False)),
        "shorts_tiers": body.get("shorts_tiers", []),
        "channel_id": body.get("channel_id") or None,
        "pipeline_type": body.get("pipeline_type", "video"),
    }

    if resume_from == "Script":
        payload.update(research_meta)
    elif resume_from == "AudioVisuals":
        payload.update(research_meta)
        payload.update(script_meta)
    elif resume_from == "Editor":
        payload.update(script_meta)
        payload["mixed_audio_s3_key"] = f"{run_id}/audio/mixed_audio.wav"
    elif resume_from == "Thumbnail":
        payload.update(script_meta)
        payload["final_video_s3_key"] = f"{run_id}/review/final_video.mp4"
        payload["video_duration_sec"] = script_meta.get("total_duration_estimate", 600)
    elif resume_from == "Upload":
        payload.update(script_meta)
        payload["final_video_s3_key"] = f"{run_id}/review/final_video.mp4"
        payload["video_duration_sec"] = script_meta.get("total_duration_estimate", 600)
        thumb_keys = [
            f"{run_id}/review/thumbnail_{i}.jpg"
            for i in range(3)
            if s3_exists(OUTPUTS_BUCKET, f"{run_id}/review/thumbnail_{i}.jpg")
        ]
        payload["thumbnail_s3_keys"] = thumb_keys
        payload["primary_thumbnail_s3_key"] = thumb_keys[0] if thumb_keys else ""
    elif resume_from == "Notify":
        payload.update(script_meta)
        payload["final_video_s3_key"] = f"{run_id}/review/final_video.mp4"
        payload["video_duration_sec"] = script_meta.get("total_duration_estimate", 600)
        thumb_keys = [
            f"{run_id}/review/thumbnail_{i}.jpg"
            for i in range(3)
            if s3_exists(OUTPUTS_BUCKET, f"{run_id}/review/thumbnail_{i}.jpg")
        ]
        payload["thumbnail_s3_keys"] = thumb_keys
        payload["primary_thumbnail_s3_key"] = thumb_keys[0] if thumb_keys else ""

    exec_name = f"resume-{run_id[:8]}-{str(uuid.uuid4())[:8]}"
    try:
        execution = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(payload),
        )
        return _response(200, {
            "run_id": exec_name,
            "original_run_id": run_id,
            "resume_from": resume_from,
            "completed_steps": completed,
            "execution_arn": execution["executionArn"],
        })
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _handle_health() -> dict:
    checks = {}

    try:
        sfn.describe_state_machine(stateMachineArn=STATE_MACHINE_ARN)
        checks["step_functions"] = "ok"
    except Exception:
        checks["step_functions"] = "error"

    try:
        s3.head_bucket(Bucket=OUTPUTS_BUCKET)
        checks["s3"] = "ok"
    except Exception:
        checks["s3"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    return _response(
        200 if all_ok else 503,
        {"status": "healthy" if all_ok else "degraded", "checks": checks},
    )


def _handle_run_list() -> dict:
    """List recent pipeline executions across RUNNING, SUCCEEDED, FAILED."""
    try:
        all_runs = []
        for status_filter in ("RUNNING", "SUCCEEDED", "FAILED"):
            resp = sfn.list_executions(
                stateMachineArn=STATE_MACHINE_ARN,
                statusFilter=status_filter,
                maxResults=15,
            )
            for ex in resp.get("executions", []):
                start = ex.get("startDate")
                stop = ex.get("stopDate")
                elapsed = None
                if start:
                    end_ts = stop or datetime.now(start.tzinfo)
                    elapsed = round((end_ts - start).total_seconds(), 1)
                all_runs.append({
                    "run_id": ex["name"],
                    "status": ex["status"],
                    "start_date": start.isoformat() if start else None,
                    "stop_date": stop.isoformat() if stop else None,
                    "elapsed_sec": elapsed,
                })
        all_runs.sort(key=lambda r: r.get("start_date") or "", reverse=True)
        return _response(200, {"runs": all_runs[:30]})
    except Exception as exc:
        log.exception("runs list error")
        return _response(500, {"error": str(exc)})


def lambda_handler(event: dict, context) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_params = event.get("pathParameters") or {}

    if method == "OPTIONS":
        return _response(200, {})

    if method == "GET" and path == "/health":
        return _handle_health()

    if method == "GET" and path == "/runs":
        return _handle_run_list()

    if not _check_api_key(event):
        return _response(401, {"error": "Missing or invalid x-api-key header"})

    if method == "POST" and path == "/run":
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            return _response(400, {"error": "Request body must be a JSON object"})
        return _handle_run(body)

    elif method == "POST" and path == "/resume":
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            return _response(400, {"error": "Request body must be a JSON object"})
        return _handle_resume(body)

    elif method == "GET" and "/status/" in path:
        run_id = path_params.get("run_id", path.split("/status/")[-1])
        return _handle_status(run_id)

    elif method == "POST" and "/stop/" in path:
        run_id = path_params.get("run_id", path.split("/stop/")[-1])
        return _handle_stop(run_id)

    elif method == "GET" and "/outputs/" in path:
        run_id = path_params.get("run_id", path.split("/outputs/")[-1])
        return _handle_outputs(run_id)

    # ── Channel CRUD routes (FIX 4) ──
    elif method == "GET" and path == "/channel/voices":
        return _handle_voices_list()

    elif method == "POST" and "/channel/" in path and path.endswith("/setup"):
        channel_id = path_params.get("id", "")
        if not channel_id:
            parts = path.split("/channel/")[-1].split("/")
            channel_id = parts[0] if parts else ""
        return _handle_channel_setup_trigger(channel_id)

    elif method == "POST" and path == "/channel/create":
        body = json.loads(event.get("body") or "{}")
        return _handle_channel_create(body)

    elif method == "GET" and path == "/channel/list":
        params = event.get("queryStringParameters") or {}
        return _handle_channel_list(params)

    elif method == "GET" and "/channel/" in path and path.endswith("/videos"):
        channel_id = path_params.get("id", "")
        if not channel_id:
            parts = path.split("/channel/")[-1].split("/")
            channel_id = parts[0] if parts else ""
        return _handle_channel_videos(channel_id)

    elif method == "PUT" and "/channel/" in path and path.endswith("/brand"):
        channel_id = path_params.get("id", "")
        if not channel_id:
            parts = path.split("/channel/")[-1].split("/")
            channel_id = parts[0] if parts else ""
        body = json.loads(event.get("body") or "{}")
        return _handle_channel_brand_update(channel_id, body)

    elif method == "DELETE" and "/channel/" in path:
        channel_id = path_params.get("id", "")
        if not channel_id:
            channel_id = path.split("/channel/")[-1].rstrip("/")
        return _handle_channel_delete(channel_id)

    elif method == "GET" and "/channel/" in path:
        channel_id = path_params.get("id", "")
        if not channel_id:
            channel_id = path.split("/channel/")[-1].rstrip("/")
            return _handle_channel_get(channel_id)

    return _response(404, {"error": "Not found"})


# ── Channel CRUD implementations ──────────────────────────────────────────────

def _handle_stop(run_id: str) -> dict:
    """Abort a running Step Functions execution."""
    try:
        exec_arn = _execution_arn(run_id)
        sfn.stop_execution(
            executionArn=exec_arn,
            error="ManualStop",
            cause="Stopped by user via Nexus dashboard",
        )
        return _response(200, {"stopped": True, "run_id": run_id})
    except sfn.exceptions.ExecutionDoesNotExist:
        return _response(404, {"error": "run not found"})
    except Exception as exc:
        log.exception("stop execution error")
        return _response(500, {"error": str(exc)})


def _handle_channel_list(params: dict) -> dict:
    try:
        db.bootstrap_schema()
        status_filter = params.get("status") or None
        channels = db.list_channels(status_filter=status_filter)
        return _response(200, {"channels": [_enrich_channel(c) for c in channels]})
    except Exception as exc:
        log.exception("channel list error")
        return _response(500, {"error": str(exc)})


def _handle_channel_create(body: dict) -> dict:
    name = (body.get("name") or body.get("channel_name") or "").strip()
    niche = (body.get("niche") or "").strip()
    profile = body.get("profile", "documentary")
    if not name or not niche:
        return _response(400, {"error": "name and niche are required"})
    if profile not in ("documentary", "finance", "entertainment"):
        return _response(400, {"error": "profile must be documentary, finance, or entertainment"})
    try:
        db.bootstrap_schema()
        channel_id = f"ch_{uuid.uuid4().hex[:12]}"
        channel = db.create_channel(
            channel_id=channel_id,
            name=name,
            niche=niche,
            profile=profile,
            style_hints=body.get("style_hints", ""),
            schedule=body.get("schedule"),
        )
        # Trigger channel setup asynchronously (brand design, logo, intro/outro)
        try:
            lam = boto3.client("lambda")
            lam.invoke(
                FunctionName=CHANNEL_SETUP_FUNCTION,
                InvocationType="Event",
                Payload=json.dumps({
                    "channel_id": channel_id,
                    "channel_name": name,
                    "niche": niche,
                    "profile": profile,
                    "style_hints": body.get("style_hints", ""),
                }).encode(),
            )
        except Exception as setup_exc:
            log.warning("Failed to auto-trigger channel setup: %s", setup_exc)
        return _response(201, {"channel": channel})
    except Exception as exc:
        log.exception("channel create error")
        return _response(500, {"error": str(exc)})


def _handle_channel_get(channel_id: str) -> dict:
    if not channel_id:
        return _response(400, {"error": "channel_id is required"})
    try:
        db.bootstrap_schema()
        channel = db.get_channel(channel_id)
        if channel is None:
            return _response(404, {"error": "Channel not found"})
        return _response(200, {"channel": _enrich_channel(channel)})
    except Exception as exc:
        log.exception("channel get error")
        return _response(500, {"error": str(exc)})


def _handle_channel_delete(channel_id: str) -> dict:
    if not channel_id:
        return _response(400, {"error": "channel_id is required"})
    try:
        db.bootstrap_schema()
        deleted = db.archive_channel(channel_id)
        if not deleted:
            return _response(404, {"error": "Channel not found"})
        return _response(200, {"deleted": True, "channel_id": channel_id})
    except Exception as exc:
        log.exception("channel delete error")
        return _response(500, {"error": str(exc)})


def _handle_channel_brand_update(channel_id: str, body: dict) -> dict:
    if not channel_id:
        return _response(400, {"error": "channel_id is required"})
    incoming_brand = body.get("brand") or {}
    voice_id = body.get("voice_id", "")
    status = body.get("status", "")
    try:
        db.bootstrap_schema()
        # Fetch existing channel to merge brand (avoid wiping logo/colors when only voice_id is sent)
        existing = db.get_channel(channel_id)
        if existing is None:
            return _response(404, {"error": "Channel not found"})
        existing_brand = existing.get("brand") or {}
        if isinstance(existing_brand, str):
            try:
                existing_brand = json.loads(existing_brand)
            except Exception:
                existing_brand = {}
        merged_brand = {**existing_brand, **incoming_brand}
        effective_voice = voice_id or existing.get("voice_id", "")
        effective_status = status or existing.get("status", "active")
        channel = db.update_channel_brand(channel_id, brand=merged_brand, voice_id=effective_voice, status=effective_status)
        if channel is None:
            return _response(404, {"error": "Channel not found"})
        return _response(200, {"channel": _enrich_channel(channel)})
    except Exception as exc:
        log.exception("channel brand update error")
        return _response(500, {"error": str(exc)})


def _handle_channel_videos(channel_id: str) -> dict:
    if not channel_id:
        return _response(400, {"error": "channel_id is required"})
    try:
        db.bootstrap_schema()
        videos = db.get_channel_videos(channel_id)
        return _response(200, {"videos": videos})
    except Exception as exc:
        log.exception("channel videos error")
        return _response(500, {"error": str(exc)})


# ── Voices ─────────────────────────────────────────────────────────────────────

def _handle_voices_list() -> dict:
    sm = boto3.client("secretsmanager")
    try:
        secret = json.loads(sm.get_secret_value(SecretId="nexus/elevenlabs_api_key")["SecretString"])
        api_key = secret.get("api_key", "")
    except Exception:
        return _response(503, {"error": "ElevenLabs API key not configured"})

    if not api_key:
        return _response(503, {"error": "ElevenLabs API key is empty"})

    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — URL is hardcoded, not user-supplied
            data = json.loads(resp.read())
        voices = [
            {
                "voice_id": v["voice_id"],
                "name": v["name"],
                "preview_url": v.get("preview_url", ""),
                "labels": v.get("labels", {}),
                "category": v.get("category", ""),
                "description": v.get("description", ""),
            }
            for v in data.get("voices", [])
        ]
        return _response(200, {"voices": voices})
    except Exception as exc:
        log.warning("ElevenLabs voices fetch failed: %s", exc)
        return _response(502, {"error": "Failed to fetch voices from ElevenLabs"})


# ── Channel Setup Trigger ───────────────────────────────────────────────────────

def _handle_channel_setup_trigger(channel_id: str) -> dict:
    if not channel_id:
        return _response(400, {"error": "channel_id is required"})
    try:
        db.bootstrap_schema()
        channel = db.get_channel(channel_id)
        if not channel:
            return _response(404, {"error": "Channel not found"})
        lam = boto3.client("lambda")
        payload = {
            "channel_id": channel_id,
            "channel_name": channel.get("name", ""),
            "niche": channel.get("niche", ""),
            "profile": channel.get("profile", "documentary"),
            "style_hints": channel.get("style_hints", ""),
        }
        lam.invoke(
            FunctionName=CHANNEL_SETUP_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        return _response(200, {"started": True, "channel_id": channel_id})
    except Exception as exc:
        log.exception("channel setup trigger error")
        return _response(500, {"error": str(exc)})
