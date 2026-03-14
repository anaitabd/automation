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


def _make_sqs_event(run_id="run-001", s3_key="run-001/review/final_video.mp4",
                    task_token="token-123", **metadata_overrides):
    metadata = {
        "title": "Test Video",
        "description": "Test description",
        "tags": [],
        "dry_run": False,
        "profile": "documentary",
        "niche": "technology",
        "primary_thumbnail_s3_key": "run-001/thumbnails/thumbnail_0.jpg",
        "thumbnail_s3_keys": ["run-001/thumbnails/thumbnail_0.jpg"],
        "video_duration_sec": 600.0,
    }
    metadata.update(metadata_overrides)
    body = {"run_id": run_id, "s3_key": s3_key, "metadata": metadata, "task_token": task_token}
    return {"Records": [{"body": json.dumps(body)}]}


def _get_success_output(sfn_mock):
    call_kwargs = sfn_mock.send_task_success.call_args[1]
    return json.loads(call_kwargs["output"])


class TestDryRun:
    def test_dry_run_returns_stub_video_id(self):
        h = _load()
        sfn_mock = MagicMock()
        with patch("boto3.client", return_value=sfn_mock):
            h.lambda_handler(_make_sqs_event(dry_run=True), None)
        result = _get_success_output(sfn_mock)
        assert result["video_id"] == "DRY_RUN_VIDEO_ID"
        assert result["dry_run"] is True

    def test_dry_run_url_is_stub(self):
        h = _load()
        sfn_mock = MagicMock()
        with patch("boto3.client", return_value=sfn_mock):
            h.lambda_handler(_make_sqs_event(dry_run=True), None)
        result = _get_success_output(sfn_mock)
        assert "DRY_RUN" in result["video_url"]

    def test_dry_run_preserves_run_id(self):
        h = _load()
        sfn_mock = MagicMock()
        with patch("boto3.client", return_value=sfn_mock):
            h.lambda_handler(_make_sqs_event(run_id="my-run", dry_run=True), None)
        result = _get_success_output(sfn_mock)
        assert result["run_id"] == "my-run"

    def test_dry_run_preserves_s3_keys(self):
        h = _load()
        sfn_mock = MagicMock()
        with patch("boto3.client", return_value=sfn_mock):
            h.lambda_handler(_make_sqs_event(s3_key="run-001/review/final_video.mp4", dry_run=True), None)
        result = _get_success_output(sfn_mock)
        assert result["final_video_s3_key"] == "run-001/review/final_video.mp4"


class TestManualApprovalMode:
    def test_pending_approval_returned_when_auto_publish_false(self):
        h = _load()
        sfn_mock = MagicMock()
        s3_mock = MagicMock()

        def client_factory(service, **kwargs):
            if service == "stepfunctions":
                return sfn_mock
            return s3_mock

        with patch("boto3.client", side_effect=client_factory), \
             patch.dict(os.environ, {"YOUTUBE_AUTO_PUBLISH": "false"}):
            h.lambda_handler(_make_sqs_event(dry_run=False), None)
        result = _get_success_output(sfn_mock)
        assert result["video_id"] == "PENDING_MANUAL_APPROVAL"

    def test_pending_approval_writes_json_to_s3(self):
        h = _load()
        sfn_mock = MagicMock()
        s3_mock = MagicMock()

        def client_factory(service, **kwargs):
            if service == "stepfunctions":
                return sfn_mock
            return s3_mock

        with patch("boto3.client", side_effect=client_factory), \
             patch.dict(os.environ, {"YOUTUBE_AUTO_PUBLISH": "false"}):
            h.lambda_handler(_make_sqs_event(dry_run=False), None)
        put_calls = [c for c in s3_mock.put_object.call_args_list
                     if "pending_upload.json" in str(c)]
        assert len(put_calls) > 0

    def test_description_available_in_pending_payload(self):
        h = _load()
        sfn_mock = MagicMock()
        s3_mock = MagicMock()
        put_calls = []

        def capturing_put(**kwargs):
            put_calls.append(kwargs)
            return {}

        s3_mock.put_object.side_effect = capturing_put

        def client_factory(service, **kwargs):
            if service == "stepfunctions":
                return sfn_mock
            return s3_mock

        with patch("boto3.client", side_effect=client_factory), \
             patch.dict(os.environ, {"YOUTUBE_AUTO_PUBLISH": "false"}):
            h.lambda_handler(_make_sqs_event(dry_run=False, description="Base desc"), None)
        pending_calls = [c for c in put_calls if "pending_upload.json" in c.get("Key", "")]
        assert len(pending_calls) > 0, "pending_upload.json was not written to S3"


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

