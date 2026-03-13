import json
import os
import time
import uuid
import boto3

import db as channel_db

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets-670294435884")
ECS_SUBNETS = json.loads(os.environ.get("ECS_SUBNETS", "[]"))
CHANNEL_SETUP_FUNCTION = os.environ.get("CHANNEL_SETUP_FUNCTION", "nexus-channel-setup")

sfn = boto3.client("stepfunctions")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

PIPELINE_STEPS = ["Research", "Script", "Audio", "Visuals", "Editor", "Thumbnail", "Upload", "Notify"]

# Bootstrap DB schema on cold start
try:
    channel_db.bootstrap_schema()
except Exception as _bootstrap_err:
    print(f"[WARN] DB bootstrap failed (may retry on first request): {_bootstrap_err}")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _execution_arn(run_id: str) -> str:
    """Build execution ARN directly from run_id (which is the execution name)."""
    return f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:{run_id}"


def _build_step_history(execution_arn: str) -> list[dict]:
    """Parse Step Functions execution history into a per-step timeline."""
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

    # Track step enter/exit times and errors
    step_data = {}  # stepName -> {entered_at, exited_at, status, error}
    current_step = None

    for ev in events:
        etype = ev.get("type", "")
        ts = ev.get("timestamp")
        ts_str = ts.isoformat() if ts else None

        if "TaskStateEntered" in etype:
            name = ev.get("stateEnteredEventDetails", {}).get("name", "")
            if name and name not in ("NotifyError", "PipelineFailed"):
                step_data[name] = {
                    "name": name,
                    "status": "running",
                    "entered_at": ts_str,
                    "exited_at": None,
                    "duration_sec": None,
                    "error": None,
                }
                current_step = name

        elif "TaskStateExited" in etype:
            name = ev.get("stateExitedEventDetails", {}).get("name", "")
            if name in step_data and step_data[name]["status"] == "running":
                step_data[name]["status"] = "done"
                step_data[name]["exited_at"] = ts_str
                if step_data[name]["entered_at"] and ts:
                    from datetime import datetime
                    try:
                        entered = datetime.fromisoformat(step_data[name]["entered_at"])
                        step_data[name]["duration_sec"] = round((ts - entered).total_seconds(), 1)
                    except Exception:
                        pass

        elif "TaskFailed" in etype:
            details = ev.get("taskFailedEventDetails", {})
            error_type = details.get("error", "Unknown")
            cause_raw = details.get("cause", "")
            # Try to parse cause as JSON for structured error info
            error_msg = cause_raw
            stack_trace = ""
            try:
                cause_json = json.loads(cause_raw)
                error_msg = cause_json.get("errorMessage", cause_raw)
                error_type = cause_json.get("errorType", error_type)
                stack_trace = "\n".join(cause_json.get("stackTrace", []))
            except (json.JSONDecodeError, TypeError):
                pass

            # Attach error to the current running step
            if current_step and current_step in step_data:
                step_data[current_step]["status"] = "error"
                step_data[current_step]["exited_at"] = ts_str
                step_data[current_step]["error"] = {
                    "type": error_type,
                    "message": error_msg[:500],
                    "stack_trace": stack_trace[:1000] if stack_trace else None,
                }
                if step_data[current_step]["entered_at"] and ts:
                    from datetime import datetime
                    try:
                        entered = datetime.fromisoformat(step_data[current_step]["entered_at"])
                        step_data[current_step]["duration_sec"] = round((ts - entered).total_seconds(), 1)
                    except Exception:
                        pass

        elif etype == "FailStateEntered":
            name = ev.get("stateEnteredEventDetails", {}).get("name", "")
            if name == "PipelineFailed":
                pass  # handled via execution status

    # Build ordered list following pipeline step order
    steps = []
    for step_name in PIPELINE_STEPS:
        if step_name in step_data:
            steps.append(step_data[step_name])
        else:
            # Step hasn't been reached yet
            steps.append({
                "name": step_name,
                "status": "pending",
                "entered_at": None,
                "exited_at": None,
                "duration_sec": None,
                "error": None,
            })

    return steps


def _handle_run(body: dict) -> dict:
    niche = body.get("niche", "")
    profile = body.get("profile", "documentary")
    dry_run = bool(body.get("dry_run", False))
    channel_id = body.get("channel_id", "")
    generate_shorts = bool(body.get("generate_shorts", False))
    shorts_tiers = body.get("shorts_tiers", "")

    if not niche:
        return _response(400, {"error": "niche is required"})
    if profile not in ("documentary", "finance", "entertainment"):
        return _response(400, {"error": "profile must be documentary|finance|entertainment"})

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
                "channel_id": channel_id,
                "generate_shorts": generate_shorts,
                "shorts_tiers": shorts_tiers,
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

    STEP_ORDER = ["Research", "Script", "AudioVisuals", "Editor", "Thumbnail", "Notify"]
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


# ═══════════════════════════════════════════════════════════════════════
# Channel CRUD handlers
# ═══════════════════════════════════════════════════════════════════════

def _handle_channel_create(body: dict) -> dict:
    """POST /channel/create — create channel + trigger async setup."""
    name = body.get("channel_name", "").strip()
    niche = body.get("niche", "").strip()
    profile = body.get("profile", "documentary")
    style_hints = body.get("style_hints", "")
    schedule = body.get("schedule")

    if not name:
        return _response(400, {"error": "channel_name is required"})
    if not niche:
        return _response(400, {"error": "niche is required"})
    if profile not in ("documentary", "finance", "entertainment"):
        return _response(400, {"error": "profile must be documentary|finance|entertainment"})

    channel_id = str(uuid.uuid4())
    try:
        ch = channel_db.create_channel(
            channel_id=channel_id,
            name=name,
            niche=niche,
            profile=profile,
            style_hints=style_hints,
            schedule=schedule,
        )
    except Exception as exc:
        return _response(500, {"error": f"Failed to create channel: {exc}"})

    # Trigger async channel setup (brand design → logo → intro/outro)
    try:
        lambda_client.invoke(
            FunctionName=CHANNEL_SETUP_FUNCTION,
            InvocationType="Event",  # async fire-and-forget
            Payload=json.dumps({
                "channel_id": channel_id,
                "channel_name": name,
                "niche": niche,
                "profile": profile,
                "style_hints": style_hints,
            }),
        )
    except Exception as exc:
        print(f"[WARN] Failed to invoke channel-setup: {exc}")
        # Channel row exists; setup can be retried

    return _response(200, ch)


def _handle_channel_list() -> dict:
    """GET /channel/list — return all non-archived channels."""
    try:
        channels = channel_db.list_channels()
        return _response(200, {"channels": channels})
    except Exception as exc:
        return _response(500, {"error": f"Failed to list channels: {exc}"})


def _handle_channel_get(channel_id: str) -> dict:
    """GET /channel/{id} — return single channel."""
    try:
        ch = channel_db.get_channel(channel_id)
        if not ch:
            return _response(404, {"error": "Channel not found"})
        return _response(200, ch)
    except Exception as exc:
        return _response(500, {"error": f"Failed to get channel: {exc}"})


def _handle_channel_update_brand(channel_id: str, body: dict) -> dict:
    """PUT /channel/{id}/brand — update channel settings."""
    try:
        name = body.get("channel_name")
        niche = body.get("niche")
        ch = channel_db.update_channel_settings(channel_id, name=name, niche=niche)
        if not ch:
            return _response(404, {"error": "Channel not found"})
        return _response(200, ch)
    except Exception as exc:
        return _response(500, {"error": f"Failed to update channel: {exc}"})


def _handle_channel_videos(channel_id: str) -> dict:
    """GET /channel/{id}/videos — return videos for a channel."""
    try:
        videos = channel_db.get_channel_videos(channel_id)
        return _response(200, {"videos": videos})
    except Exception as exc:
        return _response(500, {"error": f"Failed to get videos: {exc}"})


def _handle_channel_delete(channel_id: str) -> dict:
    """DELETE /channel/{id} — archive (soft-delete) a channel."""
    try:
        found = channel_db.archive_channel(channel_id)
        if not found:
            return _response(404, {"error": "Channel not found"})
        return _response(200, {"status": "archived", "channel_id": channel_id})
    except Exception as exc:
        return _response(500, {"error": f"Failed to archive channel: {exc}"})


def lambda_handler(event: dict, context) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_params = event.get("pathParameters") or {}

    # Handle CORS preflight
    if method == "OPTIONS":
        return _response(200, {})

    if method == "GET" and path == "/health":
        return _handle_health()

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

    # ── Channel routes ──
    elif method == "POST" and path == "/channel/create":
        body = json.loads(event.get("body") or "{}")
        return _handle_channel_create(body)

    elif method == "GET" and path == "/channel/list":
        return _handle_channel_list()

    elif method == "GET" and path.startswith("/channel/") and "/videos" in path:
        cid = path.replace("/channel/", "").replace("/videos", "")
        return _handle_channel_videos(cid)

    elif method == "PUT" and path.startswith("/channel/") and "/brand" in path:
        cid = path.replace("/channel/", "").replace("/brand", "")
        body = json.loads(event.get("body") or "{}")
        return _handle_channel_update_brand(cid, body)

    elif method == "DELETE" and path.startswith("/channel/"):
        cid = path.replace("/channel/", "")
        return _handle_channel_delete(cid)

    elif method == "GET" and path.startswith("/channel/"):
        cid = path_params.get("id", path.replace("/channel/", ""))
        return _handle_channel_get(cid)

    return _response(404, {"error": "Not found"})
