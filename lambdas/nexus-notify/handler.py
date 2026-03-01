import json
import os
import time
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
    embed = {
        "embeds": [
            {
                "title": f"✅ Nexus Cloud — New Video Published",
                "description": f"**{title}**",
                "color": 0x00A86B,
                "fields": [
                    {"name": "Niche", "value": niche, "inline": True},
                    {"name": "Profile", "value": profile, "inline": True},
                    {"name": "Duration", "value": f"{minutes}m {seconds}s", "inline": True},
                    {"name": "Run ID", "value": run_id, "inline": False},
                    {"name": "YouTube URL", "value": video_url, "inline": False},
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
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    video_url: str = event.get("video_url", "")
    video_id: str = event.get("video_id", "")
    title: str = event.get("title", "")
    niche: str = event.get("niche", "")
    thumbnail_s3_keys: list = event.get("thumbnail_s3_keys", [])
    primary_thumbnail_s3_key: str = event.get("primary_thumbnail_s3_key", "")
    video_duration_sec: float = float(event.get("video_duration_sec", 0))
    execution_start: float = float(event.get("execution_start_time", time.time()))
    dry_run: bool = event.get("dry_run", False)

    elapsed = time.time() - execution_start

    try:
        s3 = boto3.client("s3")

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
            except Exception:
                pass

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
