import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
import boto3
import psycopg2

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def _send_discord_error(
    webhook_url: str,
    run_id: str,
    profile: str,
    niche: str,
    error_info: dict,
) -> None:
    """Send a detailed error notification to Discord."""
    error_obj = error_info.get("Error", "Unknown")
    cause_raw = error_info.get("Cause", "")

    # Try to parse structured error from cause
    error_type = error_obj
    error_msg = cause_raw[:500]
    failed_step = "Unknown"
    stack_trace = ""
    try:
        cause_json = json.loads(cause_raw)
        error_type = cause_json.get("errorType", error_obj)
        error_msg = cause_json.get("errorMessage", cause_raw)[:500]
        traces = cause_json.get("stackTrace", [])
        if traces:
            stack_trace = "\n".join(traces)[:800]
            # Try to extract step name from stack trace
            for trace_line in traces:
                if "/var/task/handler.py" in trace_line and "lambda_handler" in trace_line:
                    failed_step = "Lambda Handler"
                    break
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    fields = [
        {"name": "Niche", "value": niche or "—", "inline": True},
        {"name": "Profile", "value": profile or "—", "inline": True},
        {"name": "Error Type", "value": f"`{error_type}`", "inline": True},
        {"name": "Run ID", "value": f"`{run_id}`", "inline": False},
        {"name": "Error Message", "value": f"```\n{error_msg[:400]}\n```", "inline": False},
    ]
    if stack_trace:
        fields.append(
            {"name": "Stack Trace", "value": f"```python\n{stack_trace[:600]}\n```", "inline": False}
        )

    embed = {
        "embeds": [
            {
                "title": "❌ Nexus Cloud — Pipeline Failed",
                "description": f"Pipeline run `{run_id[:8]}…` failed.",
                "color": 0xFF3B30,
                "fields": fields,
                "footer": {"text": "Nexus Cloud Pipeline • Error Notification"},
            }
        ]
    }
    data = json.dumps(embed).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "NexusCloud/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception:
        pass


S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")


def _send_discord(
    webhook_url: str,
    title: str,
    video_url: str,
    thumbnail_url: str,
    duration_sec: float,
    run_id: str,
    profile: str,
    niche: str,
) -> None:
    minutes = int(duration_sec // 60)
    seconds = int(duration_sec % 60)
    parsed = urllib.parse.urlparse(video_url)
    is_review = parsed.netloc not in ("youtube.com", "www.youtube.com")
    status_label = "🔍 Ready for Manual Review" if is_review else "✅ New Video Published"
    url_field_name = "Review Link (S3)" if is_review else "YouTube URL"
    embed = {
        "embeds": [
            {
                "title": f"{status_label} — Nexus Cloud",
                "description": f"**{title}**",
                "color": 0xF39C12 if is_review else 0x00A86B,
                "fields": [
                    {"name": "Niche", "value": niche, "inline": True},
                    {"name": "Profile", "value": profile, "inline": True},
                    {"name": "Duration", "value": f"{minutes}m {seconds}s", "inline": True},
                    {"name": "Run ID", "value": run_id, "inline": False},
                    {"name": url_field_name, "value": video_url or "—", "inline": False},
                ],
                "thumbnail": {"url": thumbnail_url},
                "footer": {"text": "Nexus Cloud Pipeline"},
            }
        ]
    }
    data = json.dumps(embed).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "NexusCloud/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception:
        pass


def _log_to_db(
    db_config: dict,
    run_id: str,
    niche: str,
    profile: str,
    title: str,
    duration_sec: float,
    video_url: str,
    elapsed_time: float,
) -> None:
    dbname = db_config.get("dbname") or "nexus"
    if not db_config.get("dbname"):
        print("[WARN] _log_to_db: dbname missing/empty in db_credentials, falling back to 'nexus'")
    conn = psycopg2.connect(
        host=db_config["host"],
        port=db_config.get("port", 5432),
        dbname=dbname,
        user=db_config["user"],
        password=db_config["password"],
        connect_timeout=10,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS nexus_runs (
                        id          SERIAL PRIMARY KEY,
                        run_id      TEXT UNIQUE NOT NULL,
                        niche       TEXT,
                        profile     TEXT,
                        title       TEXT,
                        duration_sec FLOAT,
                        video_url   TEXT,
                        elapsed_sec FLOAT,
                        created_at  TIMESTAMP DEFAULT NOW()
                    )
                    """,
                )
                cur.execute(
                    """
                    INSERT INTO nexus_runs
                        (run_id, niche, profile, title, duration_sec, video_url, elapsed_sec)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE
                        SET video_url = EXCLUDED.video_url,
                            elapsed_sec = EXCLUDED.elapsed_sec
                    """,
                    (run_id, niche, profile, title, duration_sec, video_url, elapsed_time),
                )
    finally:
        conn.close()


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
    run_id: str = event.get("run_id", "unknown")
    profile_name: str = event.get("profile", "documentary")
    niche: str = event.get("niche", "")
    dry_run: bool = event.get("dry_run", False)

    # ── Error mode: invoked by NotifyError state ──
    error_info = event.get("error")
    if error_info and isinstance(error_info, dict):
        try:
            if not dry_run:
                discord_webhook = get_secret("nexus/discord_webhook_url").get("url", "")
                if discord_webhook:
                    _send_discord_error(discord_webhook, run_id, profile_name, niche, error_info)
                # Write error to S3
                _write_error(run_id, "pipeline", Exception(json.dumps(error_info)[:500]))
        except Exception:
            pass
        return {
            "run_id": run_id,
            "status": "completed",
            "video_url": "",
            "elapsed_sec": 0,
            "dry_run": dry_run,
        }

    # ── Success mode: invoked by Notify state ──
    video_url: str = event.get("video_url", "")
    final_video_s3_key: str = event.get("final_video_s3_key", "")
    title: str = event.get("title", "")
    niche: str = event.get("niche", "")
    thumbnail_s3_keys: list = event.get("thumbnail_s3_keys", [])
    primary_thumbnail_s3_key: str = event.get("primary_thumbnail_s3_key", "")
    video_duration_sec: float = float(event.get("video_duration_sec", 0))
    execution_start_raw = event.get("execution_start_time", time.time())
    if isinstance(execution_start_raw, str):
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(execution_start_raw.replace("Z", "+00:00"))
            execution_start = dt.timestamp()
        except (ValueError, TypeError):
            execution_start = time.time()
    else:
        execution_start = float(execution_start_raw)
    dry_run: bool = event.get("dry_run", False)

    elapsed = time.time() - execution_start

    try:
        s3 = boto3.client("s3")

        if not video_url and final_video_s3_key and not dry_run:
            try:
                video_url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_OUTPUTS_BUCKET, "Key": final_video_s3_key},
                    ExpiresIn=604800,
                )
            except Exception:
                video_url = f"s3://{S3_OUTPUTS_BUCKET}/{final_video_s3_key}"

        thumbnail_url = ""
        if primary_thumbnail_s3_key and not dry_run:
            thumbnail_url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_OUTPUTS_BUCKET, "Key": primary_thumbnail_s3_key},
                ExpiresIn=86400,
            )

        if not dry_run:
            discord_webhook = get_secret("nexus/discord_webhook_url").get("url", "")
            if discord_webhook:
                _send_discord(
                    discord_webhook,
                    title,
                    video_url,
                    thumbnail_url,
                    video_duration_sec,
                    run_id,
                    profile_name,
                    niche,
                )

            try:
                db_config = get_secret("nexus/db_credentials")
                _log_to_db(
                    db_config, run_id, niche, profile_name, title,
                    video_duration_sec, video_url, elapsed,
                )
            except Exception as db_exc:
                print(f"[WARN] _log_to_db failed: {db_exc}")

        return {
            "run_id": run_id,
            "status": "completed",
            "video_url": video_url,
            "elapsed_sec": elapsed,
            "dry_run": dry_run,
        }

    except Exception as exc:
        _write_error(run_id, "notify", exc)
        raise
