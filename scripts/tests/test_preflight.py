import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
PREFLIGHT_PATH = os.path.join(REPO_ROOT, "lambdas", "nexus-api", "preflight.py")

_mod = None


def _load():
    global _mod
    if _mod is not None:
        return _mod
    spec = importlib.util.spec_from_file_location("nexus_preflight_test", PREFLIGHT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nexus_preflight_test"] = mod
    with patch("boto3.client"):
        spec.loader.exec_module(mod)
    _mod = mod
    return mod


def _secrets(perplexity_key="pk", elevenlabs_key="ek", pexels_key="px", discord_url="https://discord/hook"):
    return {
        "perplexity": {"api_key": perplexity_key},
        "elevenlabs": {"api_key": elevenlabs_key},
        "pexels": {"api_key": pexels_key},
        "discord": {"url": discord_url},
    }


class TestCircuitBreaker:
    def setup_method(self):
        m = _load()
        m._CIRCUIT_FAILURES.clear()
        m._CIRCUIT_OPEN_UNTIL.clear()
        m._PREFLIGHT_CACHE.clear()

    def test_circuit_opens_after_threshold(self):
        m = _load()
        for _ in range(m._CIRCUIT_THRESHOLD):
            m._record_failure("test_svc")
        assert m._is_circuit_open("test_svc")

    def test_circuit_closed_below_threshold(self):
        m = _load()
        for _ in range(m._CIRCUIT_THRESHOLD - 1):
            m._record_failure("test_svc2")
        assert not m._is_circuit_open("test_svc2")

    def test_circuit_resets_after_success(self):
        m = _load()
        for _ in range(m._CIRCUIT_THRESHOLD):
            m._record_failure("test_svc3")
        m._record_success("test_svc3")
        assert m._CIRCUIT_FAILURES.get("test_svc3") == 0


class TestCheckPerplexity:
    def setup_method(self):
        m = _load()
        m._CIRCUIT_FAILURES.clear()
        m._CIRCUIT_OPEN_UNTIL.clear()
        m._PREFLIGHT_CACHE.clear()

    def test_missing_key_returns_missing_key(self):
        m = _load()
        result = m._check_perplexity({"perplexity": {"api_key": ""}})
        assert result == "missing_key"

    def test_ok_on_non_5xx(self):
        m = _load()
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            result = m._check_perplexity(_secrets())
        assert result == "ok"

    def test_error_on_500(self):
        m = _load()
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=500)
            result = m._check_perplexity(_secrets())
        assert result == "error"

    def test_error_on_request_exception(self):
        m = _load()
        with patch("requests.get", side_effect=Exception("timeout")):
            result = m._check_perplexity(_secrets())
        assert result == "error"

    def test_circuit_open_returns_circuit_open(self):
        m = _load()
        for _ in range(m._CIRCUIT_THRESHOLD):
            m._record_failure("perplexity")
        result = m._check_perplexity(_secrets())
        assert result == "circuit_open"


class TestCheckBedrock:
    def setup_method(self):
        m = _load()
        m._CIRCUIT_FAILURES.clear()
        m._CIRCUIT_OPEN_UNTIL.clear()
        m._PREFLIGHT_CACHE.clear()

    def test_ok_when_bedrock_responds(self):
        m = _load()
        with patch("boto3.client") as mock_boto:
            mock_client = MagicMock()
            mock_boto.return_value = mock_client
            mock_client.list_foundation_models.return_value = {}
            result = m._check_bedrock()
        assert result == "ok"

    def test_error_when_bedrock_fails(self):
        m = _load()
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock(
                list_foundation_models=MagicMock(side_effect=Exception("no access"))
            )
            result = m._check_bedrock()
        assert result == "error"


class TestCheckElevenLabs:
    def setup_method(self):
        m = _load()
        m._CIRCUIT_FAILURES.clear()
        m._CIRCUIT_OPEN_UNTIL.clear()
        m._PREFLIGHT_CACHE.clear()

    def test_missing_key(self):
        m = _load()
        assert m._check_elevenlabs({"elevenlabs": {"api_key": ""}}) == "missing_key"

    def test_ok_on_200(self):
        m = _load()
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert m._check_elevenlabs(_secrets()) == "ok"

    def test_error_code_on_non_200(self):
        m = _load()
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=401)
            result = m._check_elevenlabs(_secrets())
        assert result == "error_401"


class TestCheckPexels:
    def setup_method(self):
        m = _load()
        m._CIRCUIT_FAILURES.clear()
        m._CIRCUIT_OPEN_UNTIL.clear()
        m._PREFLIGHT_CACHE.clear()

    def test_missing_key(self):
        m = _load()
        assert m._check_pexels({"pexels": {"api_key": ""}}) == "missing_key"

    def test_ok_on_200(self):
        m = _load()
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert m._check_pexels(_secrets()) == "ok"


class TestCheckDiscord:
    def test_ok_with_url(self):
        m = _load()
        assert m._check_discord({"discord": {"url": "https://hooks.discord.com/xxx"}}) == "ok"

    def test_ok_with_webhook_url(self):
        m = _load()
        assert m._check_discord({"discord": {"webhook_url": "https://hooks.discord.com/xxx"}}) == "ok"

    def test_missing_key_when_no_url(self):
        m = _load()
        assert m._check_discord({"discord": {}}) == "missing_key"


class TestRunPreflightChecks:
    def setup_method(self):
        m = _load()
        m._CIRCUIT_FAILURES.clear()
        m._CIRCUIT_OPEN_UNTIL.clear()
        m._PREFLIGHT_CACHE.clear()

    def test_ok_when_all_critical_pass(self):
        m = _load()
        with (
            patch.object(m, "_check_bedrock", return_value="ok"),
            patch.object(m, "_check_perplexity", return_value="ok"),
            patch.object(m, "_check_elevenlabs", return_value="ok"),
            patch.object(m, "_check_pexels", return_value="ok"),
            patch.object(m, "_check_discord", return_value="ok"),
        ):
            result = m.run_preflight_checks(_secrets())
        assert result["ok"] is True

    def test_not_ok_when_critical_service_fails(self):
        m = _load()
        with (
            patch.object(m, "_check_bedrock", return_value="error"),
            patch.object(m, "_check_perplexity", return_value="ok"),
            patch.object(m, "_check_elevenlabs", return_value="ok"),
            patch.object(m, "_check_pexels", return_value="ok"),
            patch.object(m, "_check_discord", return_value="ok"),
        ):
            result = m.run_preflight_checks(_secrets())
        assert result["ok"] is False

    def test_ok_when_non_critical_fails(self):
        m = _load()
        with (
            patch.object(m, "_check_bedrock", return_value="ok"),
            patch.object(m, "_check_perplexity", return_value="ok"),
            patch.object(m, "_check_elevenlabs", return_value="ok"),
            patch.object(m, "_check_pexels", return_value="error"),
            patch.object(m, "_check_discord", return_value="missing_key"),
        ):
            result = m.run_preflight_checks(_secrets())
        assert result["ok"] is True

    def test_ok_when_critical_missing_key(self):
        m = _load()
        with (
            patch.object(m, "_check_bedrock", return_value="ok"),
            patch.object(m, "_check_perplexity", return_value="missing_key"),
            patch.object(m, "_check_elevenlabs", return_value="missing_key"),
            patch.object(m, "_check_pexels", return_value="missing_key"),
            patch.object(m, "_check_discord", return_value="missing_key"),
        ):
            result = m.run_preflight_checks(_secrets())
        assert result["ok"] is True

    def test_result_is_cached(self):
        m = _load()
        call_count = {"n": 0}

        def _fake_bedrock():
            call_count["n"] += 1
            return "ok"

        with (
            patch.object(m, "_check_bedrock", side_effect=_fake_bedrock),
            patch.object(m, "_check_perplexity", return_value="ok"),
            patch.object(m, "_check_elevenlabs", return_value="ok"),
            patch.object(m, "_check_pexels", return_value="ok"),
            patch.object(m, "_check_discord", return_value="ok"),
        ):
            m.run_preflight_checks(_secrets())
            m.run_preflight_checks(_secrets())
        assert call_count["n"] == 1

    def test_cache_expires(self):
        m = _load()
        call_count = {"n": 0}

        def _fake_bedrock():
            call_count["n"] += 1
            return "ok"

        with (
            patch.object(m, "_check_bedrock", side_effect=_fake_bedrock),
            patch.object(m, "_check_perplexity", return_value="ok"),
            patch.object(m, "_check_elevenlabs", return_value="ok"),
            patch.object(m, "_check_pexels", return_value="ok"),
            patch.object(m, "_check_discord", return_value="ok"),
        ):
            m.run_preflight_checks(_secrets())
            m._PREFLIGHT_CACHE["preflight"]["ts"] = time.time() - m._PREFLIGHT_TTL_SEC - 1
            m.run_preflight_checks(_secrets())
        assert call_count["n"] == 2

    def test_checks_dict_has_all_services(self):
        m = _load()
        with (
            patch.object(m, "_check_bedrock", return_value="ok"),
            patch.object(m, "_check_perplexity", return_value="ok"),
            patch.object(m, "_check_elevenlabs", return_value="ok"),
            patch.object(m, "_check_pexels", return_value="ok"),
            patch.object(m, "_check_discord", return_value="ok"),
        ):
            result = m.run_preflight_checks(_secrets())
        assert set(result["checks"].keys()) == {"bedrock", "perplexity", "elevenlabs", "pexels", "discord"}
