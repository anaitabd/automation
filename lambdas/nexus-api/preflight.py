import logging
import time

import boto3
import requests

log = logging.getLogger("nexus-api.preflight")

_CIRCUIT_FAILURES: dict = {}
_CIRCUIT_OPEN_UNTIL: dict = {}
_CIRCUIT_THRESHOLD = 3
_CIRCUIT_RESET_SEC = 60

_PREFLIGHT_CACHE: dict = {}
_PREFLIGHT_TTL_SEC = 300


def _is_circuit_open(service: str) -> bool:
    until = _CIRCUIT_OPEN_UNTIL.get(service, 0)
    if time.time() < until:
        return True
    if until > 0:
        _CIRCUIT_FAILURES[service] = 0
        _CIRCUIT_OPEN_UNTIL[service] = 0
    return False


def _record_failure(service: str) -> None:
    _CIRCUIT_FAILURES[service] = _CIRCUIT_FAILURES.get(service, 0) + 1
    if _CIRCUIT_FAILURES[service] >= _CIRCUIT_THRESHOLD:
        _CIRCUIT_OPEN_UNTIL[service] = time.time() + _CIRCUIT_RESET_SEC
        log.warning("Circuit opened for %s", service)


def _record_success(service: str) -> None:
    _CIRCUIT_FAILURES[service] = 0
    _CIRCUIT_OPEN_UNTIL[service] = 0


def _check_perplexity(secrets: dict) -> str:
    if _is_circuit_open("perplexity"):
        return "circuit_open"
    api_key = secrets.get("perplexity", {}).get("api_key", "")
    if not api_key:
        return "missing_key"
    try:
        resp = requests.get(
            "https://api.perplexity.ai/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if resp.status_code < 500:
            _record_success("perplexity")
            return "ok"
        _record_failure("perplexity")
        return "error"
    except Exception as exc:
        log.warning("Perplexity preflight failed: %s", exc)
        _record_failure("perplexity")
        return "error"


def _check_bedrock() -> str:
    if _is_circuit_open("bedrock"):
        return "circuit_open"
    try:
        client = boto3.client("bedrock")
        client.list_foundation_models(byOutputModality="TEXT")
        _record_success("bedrock")
        return "ok"
    except Exception as exc:
        log.warning("Bedrock preflight failed: %s", exc)
        _record_failure("bedrock")
        return "error"


def _check_elevenlabs(secrets: dict) -> str:
    if _is_circuit_open("elevenlabs"):
        return "circuit_open"
    api_key = secrets.get("elevenlabs", {}).get("api_key", "")
    if not api_key:
        return "missing_key"
    try:
        resp = requests.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=5,
        )
        if resp.status_code == 200:
            _record_success("elevenlabs")
            return "ok"
        _record_failure("elevenlabs")
        return f"error_{resp.status_code}"
    except Exception as exc:
        log.warning("ElevenLabs preflight failed: %s", exc)
        _record_failure("elevenlabs")
        return "error"


def _check_pexels(secrets: dict) -> str:
    if _is_circuit_open("pexels"):
        return "circuit_open"
    api_key = secrets.get("pexels", {}).get("api_key", "")
    if not api_key:
        return "missing_key"
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search?query=nature&per_page=1",
            headers={"Authorization": api_key},
            timeout=5,
        )
        if resp.status_code == 200:
            _record_success("pexels")
            return "ok"
        _record_failure("pexels")
        return f"error_{resp.status_code}"
    except Exception as exc:
        log.warning("Pexels preflight failed: %s", exc)
        _record_failure("pexels")
        return "error"


def _check_discord(secrets: dict) -> str:
    webhook_url = (
        secrets.get("discord", {}).get("url", "")
        or secrets.get("discord", {}).get("webhook_url", "")
    )
    if not webhook_url:
        return "missing_key"
    return "ok"


def run_preflight_checks(secrets: dict) -> dict:
    cache_key = "preflight"
    cached = _PREFLIGHT_CACHE.get(cache_key)
    if cached and time.time() - cached["ts"] < _PREFLIGHT_TTL_SEC:
        return cached["result"]

    checks = {
        "bedrock": _check_bedrock(),
        "perplexity": _check_perplexity(secrets),
        "elevenlabs": _check_elevenlabs(secrets),
        "pexels": _check_pexels(secrets),
        "discord": _check_discord(secrets),
    }

    critical = {"bedrock", "perplexity", "elevenlabs"}
    ok = all(checks[svc] in ("ok", "missing_key") for svc in critical)

    result = {"ok": ok, "checks": checks}
    _PREFLIGHT_CACHE[cache_key] = {"result": result, "ts": time.time()}
    return result
