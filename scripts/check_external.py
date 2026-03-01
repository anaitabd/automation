#!/usr/bin/env python3
"""
check_external.py — Production-quality connectivity checks for Pexels API
and Discord Webhook.

Returns a structured dict:
    {
        "pexels":  {"ok": bool, "status": int|None, "error": str|None},
        "discord": {"ok": bool, "status": int|None, "error": str|None},
    }

Usage:
    from scripts.check_external import check_all
    result = check_all()            # reads from os.environ
    result = check_all(env={...})   # explicit overrides

CLI:
    python scripts/check_external.py
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from requests.exceptions import ConnectionError, Timeout

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────
MAX_RETRIES = 2
RETRY_BACKOFF_S = 1.0
REQUEST_TIMEOUT_S = 10

PEXELS_URL = "https://api.pexels.com/v1/search"
PEXELS_PARAMS = {"query": "cat", "per_page": 1}

_RETRYABLE = (Timeout, ConnectionError)

CheckResult = Dict[str, Any]


# ── Helpers ────────────────────────────────────────────────────────────
def _safe_key(value: str, visible: int = 6) -> str:
    """Return first `visible` chars + '***' — never log full secrets."""
    if len(value) <= visible:
        return "***"
    return value[:visible] + "***"


def _is_retryable(exc: Exception) -> bool:
    """True for transient network errors (timeout, connection reset)."""
    if isinstance(exc, _RETRYABLE):
        return True
    # requests wraps low-level errors; check inner cause
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause and isinstance(cause, (OSError, ConnectionResetError)):
        return True
    return False


def _with_retry(fn, *, label: str) -> CheckResult:
    """Call `fn` with up to MAX_RETRIES attempts on transient failures."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if _is_retryable(exc) and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_S * attempt
                logger.warning(
                    "%s: transient error (attempt %d/%d), retrying in %.1fs — %s",
                    label, attempt, MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
            else:
                break
    # Exhausted retries or non-retryable error
    error_msg = str(last_exc)
    logger.error("%s: FAILED — %s", label, error_msg)
    return {"ok": False, "status": None, "error": error_msg}


# ── Individual checks ─────────────────────────────────────────────────
def check_pexels(api_key: str) -> CheckResult:
    """
    GET https://api.pexels.com/v1/search?query=cat&per_page=1
    Header: Authorization: <API_KEY>  (no "Bearer" prefix)
    Success: HTTP 200
    """
    def _call() -> CheckResult:
        logger.info("Pexels: checking with key=%s", _safe_key(api_key))
        resp = requests.get(
            PEXELS_URL,
            params=PEXELS_PARAMS,
            headers={"Authorization": api_key},
            timeout=REQUEST_TIMEOUT_S,
        )
        ok = resp.status_code == 200
        result: CheckResult = {"ok": ok, "status": resp.status_code}
        if not ok:
            result["error"] = f"HTTP {resp.status_code}"
        else:
            logger.info("Pexels: OK (HTTP %d)", resp.status_code)
        return result

    return _with_retry(_call, label="Pexels")


def check_discord(webhook_url: str) -> CheckResult:
    """
    POST <webhook_url>
    Header: Content-Type: application/json  (NO Authorization header)
    Body: {"content": "webhook test"}
    Success: HTTP 204
    """
    def _call() -> CheckResult:
        logger.info("Discord: checking webhook=%s", _safe_key(webhook_url, 40))
        resp = requests.post(
            webhook_url,
            json={"content": "webhook test"},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
        )
        ok = resp.status_code == 204
        result: CheckResult = {"ok": ok, "status": resp.status_code}
        if not ok:
            result["error"] = f"HTTP {resp.status_code}"
        else:
            logger.info("Discord: OK (HTTP %d)", resp.status_code)
        return result

    return _with_retry(_call, label="Discord")


# ── Public API ─────────────────────────────────────────────────────────
def check_all(
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, CheckResult]:
    """
    Run all external connectivity checks.

    Parameters
    ----------
    env : dict, optional
        Override mapping; falls back to ``os.environ``.

    Returns
    -------
    dict  with keys ``pexels`` and ``discord``, each containing
    ``{"ok": bool, "status": int|None, "error": str|None}``.
    """
    src = env or os.environ

    pexels_key = (src.get("PEXELS_API_KEY") or "").strip()
    discord_url = (src.get("DISCORD_WEBHOOK_URL") or "").strip()

    results: Dict[str, CheckResult] = {}

    if pexels_key:
        results["pexels"] = check_pexels(pexels_key)
    else:
        results["pexels"] = {"ok": False, "status": None, "error": "PEXELS_API_KEY not set"}

    if discord_url:
        results["discord"] = check_discord(discord_url)
    else:
        results["discord"] = {"ok": False, "status": None, "error": "DISCORD_WEBHOOK_URL not set"}

    return results


# ── CLI ────────────────────────────────────────────────────────────────
def main() -> None:
    """Pretty-print results when run from the command line."""
    # Load .env if python-dotenv is available
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    results = check_all()

    print("\n" + "=" * 50)
    print("  External Connectivity Checks")
    print("=" * 50)
    for name, res in results.items():
        icon = "✅" if res["ok"] else "❌"
        status = f"HTTP {res['status']}" if res["status"] else "N/A"
        err = f" — {res['error']}" if res.get("error") else ""
        print(f"  {icon}  {name:<10}  {status}{err}")
    print("=" * 50 + "\n")

    # non-zero exit if any check failed
    if not all(r["ok"] for r in results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()


