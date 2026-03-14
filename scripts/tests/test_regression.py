"""Regression tests — guards against previously identified bugs and edge cases
that were fixed and must not regress."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone, timedelta
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
os.environ.setdefault("FFMPEG_BIN", "/usr/bin/ffmpeg")
os.environ.setdefault("FFPROBE_BIN", "/usr/bin/ffprobe")

_API_MOD = None


def _load_api():
    global _API_MOD
    if _API_MOD is not None:
        return _API_MOD
    spec = importlib.util.spec_from_file_location(
        "nexus_api_regression_test",
        os.path.join(LAMBDAS_DIR, "nexus-api", "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nexus_api_regression_test"] = mod
    api_dir = os.path.join(LAMBDAS_DIR, "nexus-api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    with patch("boto3.client"):
        spec.loader.exec_module(mod)
    _API_MOD = mod
    return mod


def _make_utils_mock():
    m = MagicMock()
    m.get_logger.return_value = MagicMock()
    m.notify_step_start.return_value = 0.0
    m.notify_step_complete.return_value = None
    return m


class TestGenerateShortsForwardedToSFN:
    """FIX: generate_shorts, shorts_tiers, channel_id must be forwarded to SFN input."""

    def _setup(self):
        h = _load_api()
        sfn_mock = MagicMock()
        sfn_mock.start_execution.return_value = {"executionArn": "arn:test"}
        h.sfn = sfn_mock
        return h, sfn_mock

    def _get_sfn_input(self, sfn_mock):
        call_kwargs = sfn_mock.start_execution.call_args
        kwargs = call_kwargs.kwargs or call_kwargs[1] if call_kwargs[1] else {}
        if not kwargs:
            kwargs = {"input": call_kwargs[0][0] if call_kwargs[0] else "{}"}
        return json.loads(kwargs.get("input", "{}"))

    def test_generate_shorts_true_forwarded(self):
        h, sfn_mock = self._setup()
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            h._handle_run({
                "niche": "tech", "profile": "documentary",
                "generate_shorts": True, "shorts_tiers": "micro,short",
            })
        data = self._get_sfn_input(sfn_mock)
        assert data["generate_shorts"] is True
        assert data["shorts_tiers"] == "micro,short"

    def test_generate_shorts_false_forwarded(self):
        h, sfn_mock = self._setup()
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            h._handle_run({"niche": "tech", "profile": "documentary", "generate_shorts": False})
        data = self._get_sfn_input(sfn_mock)
        assert data["generate_shorts"] is False

    def test_channel_id_forwarded_when_provided(self):
        h, sfn_mock = self._setup()
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            h._handle_run({"niche": "tech", "profile": "documentary", "channel_id": "ch-xyz"})
        data = self._get_sfn_input(sfn_mock)
        assert data["channel_id"] == "ch-xyz"

    def test_channel_id_none_when_not_provided(self):
        h, sfn_mock = self._setup()
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            h._handle_run({"niche": "tech", "profile": "documentary"})
        data = self._get_sfn_input(sfn_mock)
        assert data["channel_id"] is None

    def test_defaults_applied_when_fields_absent(self):
        h, sfn_mock = self._setup()
        with patch.object(h, "_load_preflight_secrets", return_value={}), \
             patch.object(h.preflight, "run_preflight_checks", return_value={"ok": True, "checks": {}}):
            h._handle_run({"niche": "tech", "profile": "documentary"})
        data = self._get_sfn_input(sfn_mock)
        assert data["generate_shorts"] is False
        assert data["shorts_tiers"] == "micro,short,mid,full"


class TestBuildStepHistorySkipStates:
    """FIX: Internal ASL states must be excluded from step history."""

    def test_notify_error_excluded(self):
        h = _load_api()
        h.sfn = MagicMock()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        h.sfn.get_execution_history.return_value = {"events": [
            {
                "type": "TaskStateEntered", "timestamp": ts,
                "stateEnteredEventDetails": {"name": "NotifyError"},
                "stateExitedEventDetails": {}, "taskFailedEventDetails": {},
            }
        ]}
        steps = h._build_step_history("arn:test")
        names = [s["name"] for s in steps]
        assert "NotifyError" not in names

    def test_merge_states_excluded(self):
        h = _load_api()
        h.sfn = MagicMock()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for skip_state in ["MergeParallelOutputs", "MergeContentOutputs", "SetAudioKeys"]:
            h.sfn.get_execution_history.return_value = {"events": [
                {
                    "type": "TaskStateEntered", "timestamp": ts,
                    "stateEnteredEventDetails": {"name": skip_state},
                    "stateExitedEventDetails": {}, "taskFailedEventDetails": {},
                }
            ]}
            steps = h._build_step_history("arn:test")
            names = [s["name"] for s in steps]
            assert skip_state not in names, f"Skip state {skip_state!r} not excluded"

    def test_all_expected_steps_present_in_history(self):
        h = _load_api()
        h.sfn = MagicMock()
        h.sfn.get_execution_history.return_value = {"events": []}
        steps = h._build_step_history("arn:test")
        step_names = {s["name"] for s in steps}
        for expected in ["Research", "Script", "Editor", "Notify"]:
            assert expected in step_names


class TestPipelineStepsOrder:
    """FIX: PIPELINE_STEPS must include Shorts and be ordered correctly."""

    def test_pipeline_steps_contains_shorts(self):
        h = _load_api()
        assert "Shorts" in h.PIPELINE_STEPS

    def test_pipeline_steps_are_ordered(self):
        h = _load_api()
        steps = h.PIPELINE_STEPS
        assert steps.index("Research") < steps.index("Script")
        assert steps.index("Script") < steps.index("Editor")
        assert steps.index("Editor") < steps.index("Notify")


class TestAudioHandlerPacingMarkers:
    """FIX: Audio pacing markers must be cleaned before sending to ElevenLabs."""

    def _load_audio(self):
        mod_name = "nexus_audio_regression_test"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
        with patch("boto3.client"), patch.dict(os.environ, {"FFMPEG_BIN": "/usr/bin/ffmpeg"}):
            spec = importlib.util.spec_from_file_location(
                mod_name, os.path.join(LAMBDAS_DIR, "nexus-audio", "handler.py")
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        return mod

    def test_clean_text_removes_all_markers(self):
        h = self._load_audio()
        raw = "First[PAUSE]Second[BEAT]Third[BREATH]End"
        cleaned = h._clean_text(raw)
        assert "[PAUSE]" not in cleaned
        assert "[BEAT]" not in cleaned
        assert "[BREATH]" not in cleaned

    def test_clean_text_keeps_actual_content(self):
        h = self._load_audio()
        result = h._clean_text("Hello world[PAUSE]goodbye")
        assert "Hello world" in result
        assert "goodbye" in result


class TestUploadDryRunNotCallsYouTube:
    """FIX: dry_run mode must not attempt any YouTube API calls."""

    def _load_upload(self):
        mod_name = "nexus_upload_regression_test"
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
        return mod

    def _make_sqs_event(self, run_id="r1", dry_run=True):
        body = {
            "run_id": run_id,
            "s3_key": f"{run_id}/v.mp4",
            "metadata": {
                "title": "T", "description": "", "tags": [],
                "dry_run": dry_run, "profile": "documentary", "niche": "tech",
                "primary_thumbnail_s3_key": f"{run_id}/t.jpg",
                "thumbnail_s3_keys": [f"{run_id}/t.jpg"],
                "video_duration_sec": 0.0,
            },
            "task_token": "tok-regression",
        }
        return {"Records": [{"body": json.dumps(body)}]}

    def test_dry_run_does_not_call_refresh_token(self):
        h = self._load_upload()
        sfn_mock = MagicMock()
        with patch("boto3.client", return_value=sfn_mock), \
             patch.object(h, "_refresh_access_token") as mock_refresh:
            h.lambda_handler(self._make_sqs_event(dry_run=True), None)
        mock_refresh.assert_not_called()

    def test_dry_run_does_not_call_upload_video(self):
        h = self._load_upload()
        sfn_mock = MagicMock()
        with patch("boto3.client", return_value=sfn_mock), \
             patch.object(h, "_upload_video") as mock_upload:
            h.lambda_handler(self._make_sqs_event(dry_run=True), None)
        mock_upload.assert_not_called()


class TestVisualsHandlerDoesNotCallNova:
    """FIX: dry_run in Visuals must skip all Nova Canvas / Nova Reel calls."""

    def _load_visuals(self):
        mod_name = "nexus_visuals_regression_test"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
        nova_mock = MagicMock()
        sys.modules["nova_canvas"] = nova_mock
        sys.modules["nova_reel"] = MagicMock()
        with patch("boto3.client"):
            spec = importlib.util.spec_from_file_location(
                mod_name, os.path.join(LAMBDAS_DIR, "nexus-visuals", "handler.py")
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        return mod

    def test_dry_run_does_not_call_nova_canvas(self):
        h = self._load_visuals()
        canvas_mock = MagicMock()
        reel_mock = MagicMock()
        h.nova_canvas = canvas_mock
        h.nova_reel = reel_mock
        script_data = {
            "title": "T", "mood": "neutral",
            "scenes": [{"scene_id": 0, "nova_canvas_prompt": "x", "nova_reel_prompt": "y", "estimated_duration": 6}],
        }
        profile_data = {"visuals": {"color_grade_default": "cinematic_warm"}}
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(script_data).encode())},
            {"Body": MagicMock(read=lambda: json.dumps(profile_data).encode())},
        ]
        with patch("boto3.client", return_value=s3_mock):
            h.lambda_handler(
                {"run_id": "r1", "profile": "documentary", "dry_run": True, "niche": "tech",
                 "script_s3_key": "r1/s.json"},
                None,
            )
        canvas_mock.generate_and_upload_image.assert_not_called()
        reel_mock.generate_and_upload_video.assert_not_called()


class TestScriptJsonRepair:
    """FIX: Truncated JSON from Bedrock must be repaired, not propagated as error."""

    def _load_script(self):
        mod_name = "nexus_script_regression_test"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
        with patch("boto3.client"):
            spec = importlib.util.spec_from_file_location(
                mod_name, os.path.join(LAMBDAS_DIR, "nexus-script", "handler.py")
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        return mod

    def test_complete_json_is_parsed_correctly(self):
        h = self._load_script()
        complete = '{"title": "Test", "sections": [], "total_duration_estimate": 600}'
        result = h._repair_truncated_json(complete)
        assert result["title"] == "Test"

    def test_missing_no_opening_brace_raises(self):
        h = self._load_script()
        with pytest.raises(json.JSONDecodeError):
            h._repair_truncated_json("no brace here")
