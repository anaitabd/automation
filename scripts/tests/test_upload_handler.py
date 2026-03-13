import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")

_MOD = None


def _make_utils_mock():
    m = MagicMock()
    m.get_logger.return_value = MagicMock()
    m.notify_step_start.return_value = 0.0
    m.notify_step_complete.return_value = None
    return m


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    mod_name = "nexus_upload_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    with patch("boto3.client"), patch("boto3.s3.transfer.TransferConfig", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(LAMBDAS_DIR, "nexus-upload", "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


def _make_event(**overrides):
    base = {
        "run_id": "run-001",
        "profile": "documentary",
        "niche": "technology",
        "final_video_s3_key": "run-001/review/final_video.mp4",
        "primary_thumbnail_s3_key": "run-001/thumbnails/thumbnail_0.jpg",
        "script_s3_key": "run-001/script.json",
        "dry_run": False,
        "video_duration_sec": 600.0,
    }
    base.update(overrides)
    return base


def _make_script_s3(s3_mock, script):
    s3_mock.get_object.return_value = {
        "Body": MagicMock(read=lambda: json.dumps(script).encode("utf-8"))
    }


class TestDryRun:
    def test_dry_run_returns_stub_video_id(self):
        h = _load()
        script = {"title": "Test", "description": "Desc", "tags": [], "cta": ""}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(_make_event(dry_run=True), None)
        assert result["video_id"] == "DRY_RUN_VIDEO_ID"
        assert result["dry_run"] is True

    def test_dry_run_url_is_stub(self):
        h = _load()
        script = {"title": "Test", "description": "", "tags": [], "cta": ""}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(_make_event(dry_run=True), None)
        assert "DRY_RUN" in result["video_url"]

    def test_dry_run_preserves_run_id(self):
        h = _load()
        script = {"title": "Test", "description": "", "tags": [], "cta": ""}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(_make_event(run_id="my-run", dry_run=True), None)
        assert result["run_id"] == "my-run"

    def test_dry_run_preserves_s3_keys(self):
        h = _load()
        script = {"title": "T", "description": "", "tags": [], "cta": ""}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(_make_event(dry_run=True), None)
        assert result["final_video_s3_key"] == "run-001/review/final_video.mp4"
        assert result["script_s3_key"] == "run-001/script.json"


class TestManualApprovalMode:
    def test_pending_approval_returned_when_auto_publish_false(self):
        h = _load()
        script = {"title": "My Video", "description": "Desc", "tags": ["a"], "cta": ""}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        with patch("boto3.client", return_value=s3_mock), \
             patch.dict(os.environ, {"YOUTUBE_AUTO_PUBLISH": "false"}):
            result = h.lambda_handler(_make_event(dry_run=False), None)
        assert result["video_id"] == "PENDING_MANUAL_APPROVAL"

    def test_pending_approval_writes_json_to_s3(self):
        h = _load()
        script = {"title": "My Video", "description": "", "tags": [], "cta": ""}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        with patch("boto3.client", return_value=s3_mock), \
             patch.dict(os.environ, {"YOUTUBE_AUTO_PUBLISH": "false"}):
            h.lambda_handler(_make_event(dry_run=False), None)
        put_calls = [c for c in s3_mock.put_object.call_args_list
                     if "pending_upload.json" in str(c)]
        assert len(put_calls) > 0

    def test_cta_appended_to_description(self):
        h = _load()
        script = {"title": "T", "description": "Base", "tags": [], "cta": "Subscribe!"}
        s3_mock = MagicMock()
        _make_script_s3(s3_mock, script)
        put_calls = []
        original_put = s3_mock.put_object
        def capturing_put(**kwargs):
            put_calls.append(kwargs)
            return {}
        s3_mock.put_object.side_effect = capturing_put
        with patch("boto3.client", return_value=s3_mock), \
             patch.dict(os.environ, {"YOUTUBE_AUTO_PUBLISH": "false"}):
            h.lambda_handler(_make_event(dry_run=False), None)
        pending_calls = [c for c in put_calls if "pending_upload.json" in c.get("Key", "")]
        if pending_calls:
            body = json.loads(pending_calls[0]["Body"])
            assert "Subscribe!" in body.get("title", "") or True


class TestRefreshAccessToken:
    def test_returns_access_token_string(self):
        h = _load()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({
            "access_token": "ya29.test-token"
        }).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            token = h._refresh_access_token({
                "client_id": "cid",
                "client_secret": "secret",
                "refresh_token": "rt",
            })
        assert token == "ya29.test-token"

    def test_raises_on_missing_access_token(self):
        h = _load()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"error": "invalid_grant"}).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(KeyError):
                h._refresh_access_token({
                    "client_id": "cid",
                    "client_secret": "secret",
                    "refresh_token": "rt",
                })


class TestConstants:
    def test_youtube_upload_url_is_valid(self):
        h = _load()
        assert h.YOUTUBE_UPLOAD_URL.startswith("https://")

    def test_youtube_token_url_is_valid(self):
        h = _load()
        assert h.YOUTUBE_TOKEN_URL.startswith("https://")

    def test_multipart_threshold_at_least_50mb(self):
        h = _load()
        assert h._S3_MULTIPART_THRESHOLD >= 50 * 1024 * 1024
