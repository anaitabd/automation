"""Shared pipeline utilities — Discord notifications, logging, timing.

Copied into each Lambda directory at deploy time by deploy.sh.
"""

import json
import logging
import time
import urllib.request

import boto3

# ── Step metadata ──
STEPS = {
    "research":  {"num": 1, "total": 9, "emoji": "🔍", "label": "Research"},
    "script":    {"num": 2, "total": 9, "emoji": "📝", "label": "Script"},
    "audio":     {"num": 3, "total": 9, "emoji": "🎙️", "label": "Audio"},
    "visuals":   {"num": 4, "total": 9, "emoji": "🎬", "label": "Visuals"},
    "editor":    {"num": 5, "total": 9, "emoji": "✂️", "label": "Editor"},
    "shorts":    {"num": 6, "total": 9, "emoji": "📱", "label": "Shorts"},
    "thumbnail": {"num": 7, "total": 9, "emoji": "🖼️", "label": "Thumbnail"},
    "upload":    {"num": 8, "total": 9, "emoji": "🚀", "label": "Upload"},
    "notify":    {"num": 9, "total": 9, "emoji": "🔔", "label": "Notify"},
}

_secret_cache: dict = {}


def get_logger(name: str) -> logging.Logger:
    """Return a consistently-formatted logger for any Lambda."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(name)s | %(message)s",
        force=True,
    )
    return logging.getLogger(name)


def _get_webhook_url() -> str:
    if "discord_url" not in _secret_cache:
        try:
            client = boto3.client("secretsmanager")
            secret = json.loads(
                client.get_secret_value(SecretId="nexus/discord_webhook_url")["SecretString"]
            )
            _secret_cache["discord_url"] = secret.get("url", "")
        except Exception:
            _secret_cache["discord_url"] = ""
    return _secret_cache["discord_url"]


def _post_discord(webhook_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "NexusCloud/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def notify_step_start(
    step_key: str,
    run_id: str,
    niche: str = "",
    profile: str = "",
    dry_run: bool = False,
) -> float:
    """Send a 'step started' Discord notification. Returns time.time() for timing."""
    start_time = time.time()
    meta = STEPS.get(step_key, {"num": "?", "total": 8, "emoji": "⚙️", "label": step_key})
    step_num = meta["num"]
    total = meta["total"]
    emoji = meta["emoji"]
    label = meta["label"]

    log = logging.getLogger(f"nexus-{step_key}")
    log.info("━━━ Step %d/%d: %s STARTING ━━━ run_id=%s niche=%s profile=%s dry_run=%s",
             step_num, total, label, run_id, niche, profile, dry_run)

    webhook_url = _get_webhook_url()
    if not webhook_url:
        return start_time

    dry_tag = " `[DRY RUN]`" if dry_run else ""
    embed = {
        "embeds": [{
            "title": f"{emoji} Nexus Cloud — Starting {label}",
            "description": f"Step **{step_num}/{total}**{dry_tag}",
            "color": 0x95A5A6,  # grey
            "fields": [
                {"name": "Run ID", "value": f"`{run_id}`", "inline": False},
                {"name": "Niche", "value": niche or "—", "inline": True},
                {"name": "Profile", "value": profile or "—", "inline": True},
                {"name": "Progress", "value": _progress_bar(step_num, total), "inline": False},
            ],
            "footer": {"text": "Nexus Cloud Pipeline"},
        }]
    }
    _post_discord(webhook_url, embed)
    return start_time


def notify_step_complete(
    step_key: str,
    run_id: str,
    fields: list[dict],
    elapsed_sec: float,
    dry_run: bool = False,
    color: int = 0x2ECC71,
) -> None:
    """Send a 'step completed' Discord notification with timing and details."""
    if dry_run:
        return

    meta = STEPS.get(step_key, {"num": "?", "total": 8, "emoji": "⚙️", "label": step_key})
    step_num = meta["num"]
    total = meta["total"]
    emoji = meta["emoji"]
    label = meta["label"]

    log = logging.getLogger(f"nexus-{step_key}")
    log.info("━━━ Step %d/%d: %s COMPLETED in %.1fs ━━━", step_num, total, label, elapsed_sec)

    webhook_url = _get_webhook_url()
    if not webhook_url:
        return

    # Build fields: Run ID + elapsed + progress + custom fields
    all_fields = [
        {"name": "Run ID", "value": f"`{run_id}`", "inline": False},
        {"name": "⏱ Elapsed", "value": _format_elapsed(elapsed_sec), "inline": True},
        {"name": "Step", "value": f"{step_num}/{total}", "inline": True},
    ] + fields + [
        {"name": "Progress", "value": _progress_bar(step_num + 1, total), "inline": False},
    ]

    embed = {
        "embeds": [{
            "title": f"{emoji} Nexus Cloud — {label} ✅",
            "color": color,
            "fields": all_fields,
            "footer": {"text": "Nexus Cloud Pipeline"},
        }]
    }
    _post_discord(webhook_url, embed)


def _progress_bar(current: int, total: int) -> str:
    """Generate a text-based progress bar for Discord."""
    filled = min(current, total)
    bar = "█" * filled + "░" * (total - filled)
    pct = int(filled / total * 100)
    return f"`{bar}` {pct}%"


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}m {secs}s"

