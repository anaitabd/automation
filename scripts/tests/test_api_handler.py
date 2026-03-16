"""Comprehensive tests for nexus-api/handler.py.

Covers: routing, _handle_run validation, _handle_status, _handle_outputs,
_handle_resume, _handle_health, channel CRUD, _build_step_history with
parallel states, API key checks.
"""

import json
import os
import sys
import importlib.util
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ── Setup ─────────────────────────────────────────────────────────────────────
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:nexus-pipeline")
os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("ECS_SUBNETS", "[]")
os.environ.setdefault("REQUIRE_API_KEY", "false")
os.environ.setdefault("CHANNEL_SETUP_FUNCTION", "nexus-channel-setup")

_HANDLER_MOD = None


def _load():
    global _HANDLER_MOD
    if _HANDLER_MOD is not None:
        return _HANDLER_MOD
    spec = importlib.util.spec_from_file_location(
        "nexus_api_handler_test",
        os.path.join(LAMBDAS_DIR, "nexus-api", "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nexus_api_handler_test"] = mod
    api_dir = os.path.join(LAMBDAS_DIR, "nexus-api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    with patch("boto3.client"):
        spec.loader.exec_module(mod)
    _HANDLER_MOD = mod
    return mod


# ── _handle_run ───────────────────────────────────────────────────────────────

class TestHandleRun:
    def setup_method(self):
        self.h = _load()
        self.sfn_mock = MagicMock()
        self.sfn_mock.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123:execution:test:run-id"
        }
        self.h.sfn = self.sfn_mock

    def test_forwards_all_fields(self):
        body = {
            "niche": "technology", "profile": "documentary",
            "generate_shorts": True, "shorts_tiers": "micro,short", "channel_id": "ch123",
        }
        resp = self.h._handle_run(body)
        assert resp["statusCode"] == 200
        data = json.loads((self.sfn_mock.start_execution.call_args.kwargs or self.sfn_mock.start_execution.call_args[1]).get("input", "{}"))
        assert data["generate_shorts"] is True
        assert data["shorts_tiers"] == "micro,short"
        assert data["channel_id"] == "ch123"

    def test_defaults_optional_fields(self):
        resp = self.h._handle_run({"niche": "finance", "profile": "finance"})
        assert resp["statusCode"] == 200
        data = json.loads((self.sfn_mock.start_execution.call_args.kwargs or self.sfn_mock.start_execution.call_args[1]).get("input", "{}"))
        assert data["generate_shorts"] is False
        assert data["shorts_tiers"] == []
        assert data["channel_id"] is None

    @pytest.mark.parametrize("body", [
        {"profile": "documentary"}, {}, {"niche": "", "profile": "documentary"},
    ])
    def test_returns_400_missing_niche(self, body):
        assert self.h._handle_run(body)["statusCode"] == 400

    def test_returns_400_niche_too_long(self):
        assert self.h._handle_run({"niche": "x" * 201, "profile": "documentary"})["statusCode"] == 400

    def test_returns_400_invalid_profile(self):
        assert self.h._handle_run({"niche": "tech", "profile": "invalid"})["statusCode"] == 400

    def test_returns_400_invalid_shorts_tier(self):
        assert self.h._handle_run({"niche": "tech", "profile": "documentary", "shorts_tiers": "micro,invalid"})["statusCode"] == 400

    def test_returns_400_non_bool_generate_shorts(self):
        assert self.h._handle_run({"niche": "tech", "profile": "documentary", "generate_shorts": "yes"})["statusCode"] == 400

    def test_dry_run_forwarded(self):
        self.h._handle_run({"niche": "test", "profile": "documentary", "dry_run": True})
        data = json.loads((self.sfn_mock.start_execution.call_args.kwargs or self.sfn_mock.start_execution.call_args[1]).get("input", "{}"))
        assert data["dry_run"] is True

    def test_dry_run_skips_preflight(self):
        with patch.object(self.h.preflight, "run_preflight_checks") as mock_pf:
            self.h._handle_run({"niche": "test", "profile": "documentary", "dry_run": True})
        mock_pf.assert_not_called()

    def test_preflight_failure_returns_503(self):
        with (
            patch.object(self.h, "_load_preflight_secrets", return_value={}),
            patch.object(self.h.preflight, "run_preflight_checks", return_value={"ok": False, "checks": {"bedrock": "error"}}),
        ):
            resp = self.h._handle_run({"niche": "test", "profile": "documentary"})
        assert resp["statusCode"] == 503

    def test_preflight_pass_allows_run(self):
        with (
            patch.object(self.h, "_load_preflight_secrets", return_value={}),
            patch.object(self.h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}),
        ):
            resp = self.h._handle_run({"niche": "test", "profile": "documentary"})
        assert resp["statusCode"] == 200

    def test_preflight_exception_does_not_block_run(self):
        with patch.object(self.h, "_load_preflight_secrets", side_effect=Exception("sm error")):
            resp = self.h._handle_run({"niche": "test", "profile": "documentary"})
        assert resp["statusCode"] == 200


# ── _handle_status ────────────────────────────────────────────────────────────

class TestHandleStatus:
    def _ev(self, etype, name, ts_offset=0):
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ts_offset)
        return {"type": etype, "timestamp": ts,
                "stateEnteredEventDetails": {"name": name} if "Entered" in etype else {},
                "stateExitedEventDetails": {"name": name} if "Exited" in etype else {},
                "taskFailedEventDetails": {}}

    def _setup_sfn(self, events, status="RUNNING"):
        h = _load()
        sfn_mock = MagicMock()
        sfn_mock.describe_execution.return_value = {"status": status, "startDate": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        sfn_mock.get_execution_history.return_value = {"events": events}
        sfn_mock.exceptions = MagicMock()
        sfn_mock.exceptions.ExecutionDoesNotExist = type("E", (Exception,), {})
        h.sfn = sfn_mock
        return h

    def test_running_step(self):
        h = self._setup_sfn([self._ev("TaskStateEntered", "Research", 0)])
        body = json.loads(h._handle_status("run-1")["body"])
        assert next(s for s in body["steps"] if s["name"] == "Research")["status"] == "running"

    def test_completed_step_duration(self):
        h = self._setup_sfn([self._ev("TaskStateEntered", "Research", 0), self._ev("TaskStateExited", "Research", 60)])
        body = json.loads(h._handle_status("run-1")["body"])
        r = next(s for s in body["steps"] if s["name"] == "Research")
        assert r["status"] == "done" and r["duration_sec"] == 60.0

    def test_parallel_states_tracked(self):
        h = self._setup_sfn([
            self._ev("ParallelStateEntered", "AudioVisuals", 0),
            self._ev("TaskStateEntered", "Audio", 1), self._ev("TaskStateEntered", "Visuals", 2),
        ])
        body = json.loads(h._handle_status("run-1")["body"])
        assert next(s for s in body["steps"] if s["name"] == "Audio")["status"] == "running"
        assert next(s for s in body["steps"] if s["name"] == "Visuals")["status"] == "running"

    def test_progress_100_on_succeeded(self):
        h = self._setup_sfn([], status="SUCCEEDED")
        h.sfn.describe_execution.return_value["stopDate"] = datetime(2024, 1, 1, 0, 10, tzinfo=timezone.utc)
        body = json.loads(h._handle_status("run-1")["body"])
        assert body["progress_pct"] == 100


# ── _handle_outputs ───────────────────────────────────────────────────────────

class TestHandleOutputs:
    def test_returns_presigned_urls(self):
        h = _load()
        s3m = MagicMock()
        s3m.head_object.return_value = {}
        s3m.generate_presigned_url.return_value = "https://url"
        s3m.list_objects_v2.return_value = {"Contents": []}
        h.s3 = s3m
        body = json.loads(h._handle_outputs("run-1")["body"])
        assert len(body["urls"]) > 0


# ── _handle_resume ────────────────────────────────────────────────────────────

class TestHandleResume:
    def test_400_if_no_run_id(self):
        assert _load()._handle_resume({})["statusCode"] == 400

    def test_400_for_invalid_step(self):
        assert _load()._handle_resume({"run_id": "t", "resume_from": "Bad"})["statusCode"] == 400


# ── _handle_health ────────────────────────────────────────────────────────────

class TestHandleHealth:
    def test_healthy(self):
        h = _load()
        h.sfn = MagicMock(); h.s3 = MagicMock()
        h.sfn.describe_state_machine.return_value = {}; h.s3.head_bucket.return_value = {}
        body = json.loads(h._handle_health()["body"])
        assert body["status"] == "healthy"

    def test_degraded(self):
        h = _load()
        h.sfn = MagicMock(); h.s3 = MagicMock()
        h.sfn.describe_state_machine.side_effect = Exception("boom"); h.s3.head_bucket.return_value = {}
        assert h._handle_health()["statusCode"] == 503


# ── API key ───────────────────────────────────────────────────────────────────

class TestApiKey:
    def test_passes_when_not_required(self):
        assert _load()._check_api_key({}) is True

    def test_fails_without_header_when_required(self):
        h = _load(); orig = h.REQUIRE_API_KEY; h.REQUIRE_API_KEY = True
        try:
            assert h._check_api_key({"headers": {}}) is False
        finally:
            h.REQUIRE_API_KEY = orig


# ── Routing ───────────────────────────────────────────────────────────────────

class TestRouting:
    def test_options(self):
        assert _load().lambda_handler({"httpMethod": "OPTIONS", "path": "/run"}, None)["statusCode"] == 200

    def test_unknown_404(self):
        assert _load().lambda_handler({"httpMethod": "GET", "path": "/unknown"}, None)["statusCode"] == 404


# ── _build_step_history ───────────────────────────────────────────────────────

class TestBuildStepHistory:
    def test_all_steps_present(self):
        h = _load()
        h.sfn = MagicMock()
        h.sfn.get_execution_history.return_value = {"events": []}
        names = [s["name"] for s in h._build_step_history("arn:test")]
        assert names == h.PIPELINE_STEPS

    def test_skip_states_excluded(self):
        h = _load(); h.sfn = MagicMock()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        h.sfn.get_execution_history.return_value = {"events": [
            {"type": "TaskStateEntered", "timestamp": ts, "stateEnteredEventDetails": {"name": "NotifyError"},
             "stateExitedEventDetails": {}, "taskFailedEventDetails": {}},
        ]}
        assert all(s["status"] == "pending" for s in h._build_step_history("arn:test"))
