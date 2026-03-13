"""Tests for nexus_pipeline_utils.py — shared notification + validation utilities."""

import json
import os
import sys
import importlib.util
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _load_utils():
    spec = importlib.util.spec_from_file_location(
        "nexus_pipeline_utils_test",
        os.path.join(REPO_ROOT, "lambdas", "nexus_pipeline_utils.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nexus_pipeline_utils_test"] = mod
    with patch("boto3.client", return_value=MagicMock()):
        spec.loader.exec_module(mod)
    return mod


class TestStepsDict:
    def test_all_steps_have_required_keys(self):
        u = _load_utils()
        for key, meta in u.STEPS.items():
            assert "num" in meta, f"Step '{key}' missing 'num'"
            assert "total" in meta, f"Step '{key}' missing 'total'"
            assert "emoji" in meta, f"Step '{key}' missing 'emoji'"
            assert "label" in meta, f"Step '{key}' missing 'label'"

    def test_step_numbers_unique(self):
        u = _load_utils()
        nums = [m["num"] for m in u.STEPS.values()]
        assert len(nums) == len(set(nums))

    def test_total_consistent(self):
        u = _load_utils()
        totals = {m["total"] for m in u.STEPS.values()}
        assert len(totals) == 1, "All steps should have the same total"

    def test_total_matches_step_count(self):
        u = _load_utils()
        first_total = list(u.STEPS.values())[0]["total"]
        assert first_total == len(u.STEPS)


class TestProgressBar:
    def test_empty_progress(self):
        u = _load_utils()
        bar = u._progress_bar(0, 9)
        assert "0%" in bar
        assert "░" in bar

    def test_full_progress(self):
        u = _load_utils()
        bar = u._progress_bar(9, 9)
        assert "100%" in bar
        assert "█" in bar

    def test_partial_progress(self):
        u = _load_utils()
        bar = u._progress_bar(3, 9)
        assert "33%" in bar

    def test_overcapped_at_total(self):
        u = _load_utils()
        bar = u._progress_bar(15, 9)
        assert "100%" in bar


class TestFormatElapsed:
    def test_seconds_only(self):
        u = _load_utils()
        assert u._format_elapsed(45.3) == "45.3s"

    def test_minutes_and_seconds(self):
        u = _load_utils()
        assert u._format_elapsed(125) == "2m 5s"

    def test_zero(self):
        u = _load_utils()
        assert u._format_elapsed(0) == "0.0s"


class TestGetLogger:
    def test_returns_logger_with_name(self):
        u = _load_utils()
        logger = u.get_logger("test-module")
        assert logger.name == "test-module"


class TestNotifyStepStart:
    def test_returns_float_timestamp(self):
        u = _load_utils()
        # Patch _get_webhook_url to return empty (skip Discord call)
        with patch.object(u, "_get_webhook_url", return_value=""):
            result = u.notify_step_start("research", "run-1", niche="test")
        assert isinstance(result, float)
        assert result > 0

    def test_unknown_step_key_does_not_crash(self):
        u = _load_utils()
        with patch.object(u, "_get_webhook_url", return_value=""):
            result = u.notify_step_start("unknown_step", "run-1")
        assert isinstance(result, float)


class TestNotifyStepComplete:
    def test_skips_on_dry_run(self):
        u = _load_utils()
        # Should not raise or post to Discord
        u.notify_step_complete("research", "run-1", [], elapsed_sec=10.0, dry_run=True)

    def test_posts_when_not_dry_run(self):
        u = _load_utils()
        with patch.object(u, "_get_webhook_url", return_value="https://discord.com/webhook") as wh, \
             patch.object(u, "_post_discord") as pd:
            u.notify_step_complete("research", "run-1", [], elapsed_sec=10.0, dry_run=False)
            pd.assert_called_once()


class TestValidateSecrets:
    def test_raises_when_env_vars_missing(self):
        u = _load_utils()
        # Clear all required env vars temporarily
        saved = {}
        for k in u._REQUIRED_ENV_VARS:
            saved[k] = os.environ.pop(k, None)
        try:
            with pytest.raises(EnvironmentError, match="Missing required"):
                u.validate_secrets()
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_passes_when_all_set(self):
        u = _load_utils()
        saved = {}
        for k in u._REQUIRED_ENV_VARS:
            saved[k] = os.environ.get(k)
            os.environ[k] = "test-value"
        try:
            u.validate_secrets()  # should not raise
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

