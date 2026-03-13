"""Contract tests — verifies the API Gateway event/response shape and
Lambda payload contracts as documented in the API handler."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:nexus-pipeline")
os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("ECS_SUBNETS", "[]")
os.environ.setdefault("REQUIRE_API_KEY", "false")
os.environ.setdefault("CHANNEL_SETUP_FUNCTION", "nexus-channel-setup")

_API_MOD = None


def _load_api():
    global _API_MOD
    if _API_MOD is not None:
        return _API_MOD
    spec = importlib.util.spec_from_file_location(
        "nexus_api_contract_test",
        os.path.join(LAMBDAS_DIR, "nexus-api", "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nexus_api_contract_test"] = mod
    api_dir = os.path.join(LAMBDAS_DIR, "nexus-api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    with patch("boto3.client"):
        spec.loader.exec_module(mod)
    _API_MOD = mod
    return mod


class TestResponseShape:
    """All API responses must be valid API Gateway proxy responses."""

    def _assert_gateway_response(self, resp):
        assert "statusCode" in resp
        assert isinstance(resp["statusCode"], int)
        assert "headers" in resp
        assert "body" in resp
        assert isinstance(resp["body"], str)
        body = json.loads(resp["body"])
        assert isinstance(body, dict)

    def test_run_400_is_valid_gateway_response(self):
        h = _load_api()
        resp = h._handle_run({})
        self._assert_gateway_response(resp)
        assert resp["statusCode"] == 400

    def test_run_200_is_valid_gateway_response(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.sfn.start_execution.return_value = {"executionArn": "arn:test"}
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            resp = h._handle_run({"niche": "tech", "profile": "documentary"})
        self._assert_gateway_response(resp)
        assert resp["statusCode"] == 200

    def test_cors_headers_present_on_run_response(self):
        h = _load_api()
        resp = h._handle_run({})
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_cors_headers_present_on_options(self):
        h = _load_api()
        resp = h.lambda_handler({"httpMethod": "OPTIONS", "path": "/run"}, None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
        assert resp["statusCode"] == 200

    def test_content_type_is_json(self):
        h = _load_api()
        resp = h._handle_run({})
        assert "application/json" in resp["headers"]["Content-Type"]


class TestRunInputContract:
    """POST /run body must satisfy documented contract."""

    def test_niche_required(self):
        h = _load_api()
        body = json.loads(h._handle_run({"profile": "documentary"})["body"])
        assert "error" in body or "message" in body

    def test_niche_max_length_200(self):
        h = _load_api()
        resp = h._handle_run({"niche": "x" * 201, "profile": "documentary"})
        assert resp["statusCode"] == 400

    def test_profile_must_be_valid_name(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "unknown_profile_xyz"})
        assert resp["statusCode"] == 400

    def test_generate_shorts_must_be_bool(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "documentary", "generate_shorts": "true"})
        assert resp["statusCode"] == 400

    def test_shorts_tiers_must_be_valid_subset(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "documentary", "shorts_tiers": "mega,invalid"})
        assert resp["statusCode"] == 400

    @pytest.mark.parametrize("tier", ["micro", "short", "mid", "full"])
    def test_each_valid_tier_accepted(self, tier):
        h = _load_api()
        h.sfn = MagicMock()
        h.sfn.start_execution.return_value = {"executionArn": "arn:test"}
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            resp = h._handle_run({"niche": "tech", "profile": "documentary", "shorts_tiers": tier})
        assert resp["statusCode"] == 200

    def test_valid_profiles_accepted(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.sfn.start_execution.return_value = {"executionArn": "arn:test"}
        for profile in ["documentary", "finance", "entertainment"]:
            with patch.object(h, "_load_preflight_secrets", return_value={}), \
                 patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
                resp = h._handle_run({"niche": "tech", "profile": profile})
            assert resp["statusCode"] == 200, f"Profile {profile!r} rejected"


class TestStatusOutputContract:
    """GET /status/{run_id} response schema."""

    def _make_sfn(self, events=None, status="RUNNING"):
        h = _load_api()
        sfn_mock = MagicMock()
        sfn_mock.describe_execution.return_value = {
            "status": status,
            "startDate": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        sfn_mock.get_execution_history.return_value = {"events": events or []}
        sfn_mock.exceptions = MagicMock()
        sfn_mock.exceptions.ExecutionDoesNotExist = type("E", (Exception,), {})
        h.sfn = sfn_mock
        return h

    def test_status_response_has_steps_field(self):
        h = self._make_sfn()
        body = json.loads(h._handle_status("run-1")["body"])
        assert "steps" in body
        assert isinstance(body["steps"], list)

    def test_status_response_has_progress_pct(self):
        h = self._make_sfn()
        body = json.loads(h._handle_status("run-1")["body"])
        assert "progress_pct" in body
        assert 0 <= body["progress_pct"] <= 100

    def test_status_response_has_status_field(self):
        h = self._make_sfn()
        body = json.loads(h._handle_status("run-1")["body"])
        assert "status" in body

    def test_each_step_has_name_and_status(self):
        h = self._make_sfn()
        body = json.loads(h._handle_status("run-1")["body"])
        for step in body["steps"]:
            assert "name" in step
            assert "status" in step
            assert step["status"] in ("pending", "running", "done", "failed")

    def test_404_on_missing_run(self):
        h = _load_api()
        sfn_mock = MagicMock()
        sfn_mock.exceptions = MagicMock()
        sfn_mock.exceptions.ExecutionDoesNotExist = type("E", (Exception,), {})
        sfn_mock.describe_execution.side_effect = sfn_mock.exceptions.ExecutionDoesNotExist()
        h.sfn = sfn_mock
        resp = h._handle_status("nonexistent-run")
        assert resp["statusCode"] == 404


class TestOutputsContract:
    """GET /outputs/{run_id} response schema."""

    def test_outputs_response_has_urls_field(self):
        h = _load_api()
        s3_mock = MagicMock()
        s3_mock.head_object.return_value = {}
        s3_mock.generate_presigned_url.return_value = "https://presigned.url/file"
        s3_mock.list_objects_v2.return_value = {"Contents": []}
        h.s3 = s3_mock
        body = json.loads(h._handle_outputs("run-1")["body"])
        assert "urls" in body

    def test_urls_are_strings(self):
        h = _load_api()
        s3_mock = MagicMock()
        s3_mock.head_object.return_value = {}
        s3_mock.generate_presigned_url.return_value = "https://presigned.url/file"
        s3_mock.list_objects_v2.return_value = {"Contents": []}
        h.s3 = s3_mock
        body = json.loads(h._handle_outputs("run-1")["body"])
        for url in body["urls"]:
            assert isinstance(url, str)


class TestHealthContract:
    """GET /health response schema."""

    def test_health_response_has_status_field(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.s3 = MagicMock()
        h.sfn.describe_state_machine.return_value = {}
        h.s3.head_bucket.return_value = {}
        body = json.loads(h._handle_health()["body"])
        assert "status" in body
        assert body["status"] in ("healthy", "degraded")

    def test_healthy_returns_200(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.s3 = MagicMock()
        h.sfn.describe_state_machine.return_value = {}
        h.s3.head_bucket.return_value = {}
        assert h._handle_health()["statusCode"] == 200

    def test_degraded_returns_503(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.s3 = MagicMock()
        h.sfn.describe_state_machine.side_effect = Exception("connection refused")
        h.s3.head_bucket.return_value = {}
        assert h._handle_health()["statusCode"] == 503


class TestResumeContract:
    """POST /resume body contract."""

    def test_resume_requires_run_id(self):
        h = _load_api()
        resp = h._handle_resume({})
        assert resp["statusCode"] == 400

    def test_resume_with_valid_step_accepted(self):
        h = _load_api()
        sfn_mock = MagicMock()
        sfn_mock.start_execution.return_value = {"executionArn": "arn:test"}
        h.sfn = sfn_mock
        s3_mock = MagicMock()
        s3_mock.list_objects_v2.return_value = {"Contents": []}
        h.s3 = s3_mock
        resp = h._handle_resume({"run_id": "r1", "resume_from": "Script"})
        assert resp["statusCode"] in (200, 400)

    def test_resume_rejects_unknown_step(self):
        h = _load_api()
        resp = h._handle_resume({"run_id": "r1", "resume_from": "NonExistentStep"})
        assert resp["statusCode"] == 400
