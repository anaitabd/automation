import json
import os
import time
import uuid
import boto3

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
OUTPUTS_BUCKET = os.environ["OUTPUTS_BUCKET"]

sfn = boto3.client("stepfunctions")
s3 = boto3.client("s3")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _handle_run(body: dict) -> dict:
    niche = body.get("niche", "")
    profile = body.get("profile", "documentary")
    dry_run = bool(body.get("dry_run", False))

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
        executions = sfn.list_executions(
            stateMachineArn=STATE_MACHINE_ARN,
            maxResults=1,
        )
        for ex in executions.get("executions", []):
            if ex["name"] == run_id:
                detail = sfn.describe_execution(executionArn=ex["executionArn"])
                step = _get_current_step(ex["executionArn"])
                return _response(
                    200,
                    {
                        "run_id": run_id,
                        "status": detail["status"],
                        "current_step": step,
                        "start_date": detail["startDate"].isoformat(),
                        "stop_date": detail["stopDate"].isoformat() if detail.get("stopDate") else None,
                    },
                )
        return _response(404, {"error": "run not found"})
    except Exception as exc:
        return _response(500, {"error": str(exc)})


def _get_current_step(execution_arn: str) -> str:
    try:
        history = sfn.get_execution_history(
            executionArn=execution_arn,
            maxResults=50,
            reverseOrder=True,
        )
        for event in history.get("events", []):
            et = event.get("type", "")
            if "StateEntered" in et:
                return event.get("stateEnteredEventDetails", {}).get("name", "unknown")
    except Exception:
        pass
    return "unknown"


def _handle_outputs(run_id: str) -> dict:
    output_files = [
        f"{run_id}/final_video.mp4",
        f"{run_id}/thumbnails/thumbnail_0.jpg",
        f"{run_id}/thumbnails/thumbnail_1.jpg",
        f"{run_id}/thumbnails/thumbnail_2.jpg",
        f"{run_id}/script.json",
        f"{run_id}/research.json",
    ]

    presigned_urls = {}
    for key in output_files:
        try:
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": OUTPUTS_BUCKET, "Key": key},
                ExpiresIn=3600,
            )
            presigned_urls[key] = url
        except Exception:
            presigned_urls[key] = None

    return _response(200, {"run_id": run_id, "urls": presigned_urls})


def lambda_handler(event: dict, context) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    path_params = event.get("pathParameters") or {}

    if method == "POST" and path == "/run":
        body = json.loads(event.get("body") or "{}")
        return _handle_run(body)

    elif method == "GET" and "/status/" in path:
        run_id = path_params.get("run_id", path.split("/status/")[-1])
        return _handle_status(run_id)

    elif method == "GET" and "/outputs/" in path:
        run_id = path_params.get("run_id", path.split("/outputs/")[-1])
        return _handle_outputs(run_id)

    return _response(404, {"error": "Not found"})
