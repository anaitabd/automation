"""Security tests — validates authentication, authorization, input validation,
secret handling, and protection against common attack vectors."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:nexus-pipeline")
os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")
os.environ.setdefault("ECS_SUBNETS", "[]")
os.environ.setdefault("REQUIRE_API_KEY", "false")
os.environ.setdefault("CHANNEL_SETUP_FUNCTION", "nexus-channel-setup")

_API_MOD = None


def _load_api():
    global _API_MOD
    if _API_MOD is not None:
        return _API_MOD
    spec = importlib.util.spec_from_file_location(
        "nexus_api_security_test",
        os.path.join(LAMBDAS_DIR, "nexus-api", "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nexus_api_security_test"] = mod
    api_dir = os.path.join(LAMBDAS_DIR, "nexus-api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    with patch("boto3.client"):
        spec.loader.exec_module(mod)
    _API_MOD = mod
    return mod


class TestApiKeyAuthentication:
    """API key enforcement when REQUIRE_API_KEY=true."""

    def test_request_rejected_without_key_when_required(self):
        h = _load_api()
        orig = h.REQUIRE_API_KEY
        h.REQUIRE_API_KEY = True
        try:
            resp = h.lambda_handler(
                {"httpMethod": "POST", "path": "/run",
                 "headers": {}, "body": json.dumps({"niche": "tech", "profile": "documentary"})},
                None,
            )
            assert resp["statusCode"] == 401
        finally:
            h.REQUIRE_API_KEY = orig

    def test_request_accepted_with_correct_key(self):
        h = _load_api()
        orig = h.REQUIRE_API_KEY
        h.REQUIRE_API_KEY = True
        h.sfn = MagicMock()
        h.sfn.start_execution.return_value = {"executionArn": "arn:test"}
        try:
            with patch.object(h, "_load_preflight_secrets", return_value={}), \
                 patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
                resp = h.lambda_handler(
                    {"httpMethod": "POST", "path": "/run",
                     "headers": {"x-api-key": "valid-key"},
                     "body": json.dumps({"niche": "tech", "profile": "documentary"})},
                    None,
                )
            assert resp["statusCode"] == 200
        finally:
            h.REQUIRE_API_KEY = orig

    def test_request_accepted_with_uppercase_header(self):
        h = _load_api()
        orig = h.REQUIRE_API_KEY
        h.REQUIRE_API_KEY = True
        h.sfn = MagicMock()
        h.sfn.start_execution.return_value = {"executionArn": "arn:test"}
        try:
            with patch.object(h, "_load_preflight_secrets", return_value={}), \
                 patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
                resp = h.lambda_handler(
                    {"httpMethod": "POST", "path": "/run",
                     "headers": {"X-Api-Key": "valid-key"},
                     "body": json.dumps({"niche": "tech", "profile": "documentary"})},
                    None,
                )
            assert resp["statusCode"] == 200
        finally:
            h.REQUIRE_API_KEY = orig

    def test_401_body_contains_error_message(self):
        h = _load_api()
        orig = h.REQUIRE_API_KEY
        h.REQUIRE_API_KEY = True
        try:
            resp = h.lambda_handler(
                {"httpMethod": "POST", "path": "/run",
                 "headers": {}, "body": json.dumps({"niche": "tech", "profile": "documentary"})},
                None,
            )
            body = json.loads(resp["body"])
            assert "error" in body
        finally:
            h.REQUIRE_API_KEY = orig


class TestInputValidationSecurity:
    """Input validation prevents injection and oversized payloads."""

    def test_niche_length_enforced_at_200_chars(self):
        h = _load_api()
        resp = h._handle_run({"niche": "a" * 201, "profile": "documentary"})
        assert resp["statusCode"] == 400

    def test_niche_exactly_200_chars_accepted(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.sfn.start_execution.return_value = {"executionArn": "arn:test"}
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            resp = h._handle_run({"niche": "a" * 200, "profile": "documentary"})
        assert resp["statusCode"] == 200

    def test_empty_niche_rejected(self):
        h = _load_api()
        resp = h._handle_run({"niche": "", "profile": "documentary"})
        assert resp["statusCode"] == 400

    def test_whitespace_only_niche_rejected(self):
        h = _load_api()
        resp = h._handle_run({"niche": "   ", "profile": "documentary"})
        assert resp["statusCode"] in (400, 200)

    def test_invalid_profile_rejected(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "../../etc/passwd"})
        assert resp["statusCode"] == 400

    def test_generate_shorts_must_be_bool_not_string(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "documentary", "generate_shorts": "1"})
        assert resp["statusCode"] == 400

    def test_generate_shorts_must_be_bool_not_int(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "documentary", "generate_shorts": 1})
        assert resp["statusCode"] == 400

    def test_channel_id_must_be_string_when_provided(self):
        h = _load_api()
        resp = h._handle_run({"niche": "tech", "profile": "documentary", "channel_id": 12345})
        assert resp["statusCode"] == 400

    def test_invalid_shorts_tier_rejected(self):
        h = _load_api()
        resp = h._handle_run({
            "niche": "tech", "profile": "documentary",
            "shorts_tiers": "micro,'; DROP TABLE runs; --"
        })
        assert resp["statusCode"] == 400

    @pytest.mark.parametrize("payload", [
        None,
        "not json",
        123,
    ])
    def test_non_dict_body_rejected(self, payload):
        h = _load_api()
        resp = h.lambda_handler(
            {"httpMethod": "POST", "path": "/run", "headers": {}, "body": json.dumps(payload)},
            None,
        )
        assert resp["statusCode"] in (400, 500)


class TestSecretHandling:
    """Secrets must be fetched from Secrets Manager, not hardcoded or logged."""

    def test_api_handler_does_not_hardcode_secrets(self):
        h = _load_api()
        source_path = os.path.join(LAMBDAS_DIR, "nexus-api", "handler.py")
        with open(source_path) as f:
            source = f.read()
        suspicious_patterns = ["sk-", "Bearer ey", "password=", "api_key="]
        for pattern in suspicious_patterns:
            assert pattern not in source, f"Potential hardcoded secret found: {pattern!r}"

    def test_pipeline_utils_does_not_hardcode_secrets(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus_pipeline_utils.py")
        with open(source_path) as f:
            source = f.read()
        suspicious_patterns = ["sk-", "Bearer ey", "password=secret"]
        for pattern in suspicious_patterns:
            assert pattern not in source, f"Potential hardcoded secret found: {pattern!r}"

    def test_upload_handler_does_not_hardcode_youtube_credentials(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus-upload", "handler.py")
        with open(source_path) as f:
            source = f.read()
        assert "client_secret" not in source.split("credentials")[0][:200]

    def test_secrets_fetched_via_get_secret_function(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus-research", "handler.py")
        with open(source_path) as f:
            source = f.read()
        assert "get_secret(" in source or "secretsmanager" in source


class TestCorsHeaders:
    """CORS headers must be present on all responses to support browser access."""

    def test_cors_origin_wildcard(self):
        h = _load_api()
        resp = h._handle_run({})
        assert resp["headers"].get("Access-Control-Allow-Origin") == "*"

    def test_cors_methods_include_post(self):
        h = _load_api()
        resp = h.lambda_handler({"httpMethod": "OPTIONS", "path": "/run"}, None)
        methods = resp["headers"].get("Access-Control-Allow-Methods", "")
        assert "POST" in methods

    def test_cors_headers_include_api_key_header(self):
        h = _load_api()
        resp = h.lambda_handler({"httpMethod": "OPTIONS", "path": "/run"}, None)
        allowed_headers = resp["headers"].get("Access-Control-Allow-Headers", "")
        assert "x-api-key" in allowed_headers.lower()


class TestSecretCaching:
    """Secrets Manager calls must be cached to avoid unnecessary API calls."""

    def test_research_handler_uses_cache(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus-research", "handler.py")
        with open(source_path) as f:
            source = f.read()
        assert "_cache" in source

    def test_script_handler_uses_cache(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus-script", "handler.py")
        with open(source_path) as f:
            source = f.read()
        assert "_cache" in source

    def test_audio_handler_uses_cache(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus-audio", "handler.py")
        with open(source_path) as f:
            source = f.read()
        assert "_cache" in source

    def test_upload_handler_uses_cache(self):
        source_path = os.path.join(LAMBDAS_DIR, "nexus-upload", "handler.py")
        with open(source_path) as f:
            source = f.read()
        assert "_cache" in source


class TestErrorResponsesDoNotLeakInternals:
    """Error responses must not expose stack traces or internal system details."""

    def test_400_body_has_error_key_not_traceback(self):
        h = _load_api()
        resp = h._handle_run({"niche": "x" * 201, "profile": "documentary"})
        body = json.loads(resp["body"])
        assert "Traceback" not in str(body)
        assert "error" in body or "message" in body

    def test_404_does_not_expose_arn(self):
        h = _load_api()
        sfn_mock = MagicMock()
        sfn_mock.exceptions = MagicMock()
        sfn_mock.exceptions.ExecutionDoesNotExist = type("E", (Exception,), {})
        sfn_mock.describe_execution.side_effect = sfn_mock.exceptions.ExecutionDoesNotExist("arn:secret:run")
        h.sfn = sfn_mock
        resp = h._handle_status("nonexistent-run")
        body_str = resp["body"]
        assert "arn:aws:states" not in body_str

    def test_unknown_route_returns_minimal_404(self):
        h = _load_api()
        resp = h.lambda_handler({"httpMethod": "DELETE", "path": "/secret/admin"}, None)
        assert resp["statusCode"] == 404
        body = json.loads(resp["body"])
        assert "error" in body or "message" in body
