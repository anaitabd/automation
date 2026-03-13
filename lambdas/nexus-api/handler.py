import json
import os
import time
import uuid
from datetime import datetime

import boto3

import db

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
ECS_SUBNETS = json.loads(os.environ.get("ECS_SUBNETS", "[]"))
REQUIRE_API_KEY = os.environ.get("REQUIRE_API_KEY", "false").lower() == "true"
CHANNEL_SETUP_FUNCTION = os.environ.get("CHANNEL_SETUP_FUNCTION", "nexus-channel-setup")

sfn = boto3.client("stepfunctions")
s3 = boto3.client("s3")

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


def _handle_run(body: dict) -> dict:
    validation_error = _validate_run_body(body)
    if validation_error:
        return _response(400, {"error": validation_error})

    niche = body["niche"].strip()
    profile = body.get("profile", "documentary")
    dry_run = bool(body.get("dry_run", False))
    generate_shorts = bool(body.get("generate_shorts", False))
    shorts_tiers = body.get("shorts_tiers", "micro,short,mid,full")
    channel_id = body.get("channel_id") or None

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
            "section_count": sd.get("section_count", len(sd.get("sections", []))),
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
        "shorts_tiers": body.get("shorts_tiers", "micro,short,mid,full"),
        "channel_id": body.get("channel_id") or None,
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


def lambda_handler(event: dict, context) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_params = event.get("pathParameters") or {}

    if method == "OPTIONS":
        return _response(200, {})

    if method == "GET" and path == "/health":
        return _handle_health()

    if not _check_api_key(event):
        return _response(401, {"error": "Missing or invalid x-api-key header"})

    if method == "POST" and path == "/run":
        body = json.loads(event.get("body") or "{}")
        return _handle_run(body)

    elif method == "POST" and path == "/resume":
        body = json.loads(event.get("body") or "{}")
        return _handle_resume(body)

    elif method == "GET" and "/status/" in path:
        run_id = path_params.get("run_id", path.split("/status/")[-1])
        return _handle_status(run_id)

    elif method == "GET" and "/outputs/" in path:
        run_id = path_params.get("run_id", path.split("/outputs/")[-1])
        return _handle_outputs(run_id)

    # ── Channel CRUD routes (FIX 4) ──
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
