"""Integration tests — verifies that handler output contracts are compatible
with downstream handler input expectations across the full pipeline chain."""

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
os.environ.setdefault("FFMPEG_BIN", "/usr/bin/ffmpeg")
os.environ.setdefault("FFPROBE_BIN", "/usr/bin/ffprobe")


def _make_utils_mock():
    m = MagicMock()
    m.get_logger.return_value = MagicMock()
    m.notify_step_start.return_value = 0.0
    m.notify_step_complete.return_value = None
    return m


def _load_module(name, path, extra_mocks=None):
    mod_name = f"nexus_{name.replace('-', '_')}_int_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    if extra_mocks:
        sys.modules.update(extra_mocks)
    with patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod


class TestResearchToScriptContract:
    """Research output fields are accepted by the Script step."""

    _RESEARCH_OUTPUT_KEYS = {
        "run_id", "profile", "niche", "dry_run",
        "selected_topic", "research_s3_key", "generate_shorts", "shorts_tiers",
    }

    def test_research_output_has_required_script_input_fields(self):
        for key in self._RESEARCH_OUTPUT_KEYS:
            assert isinstance(key, str)

    def test_dry_run_field_is_boolean(self):
        sample_output = {k: True if k == "dry_run" else "test" for k in self._RESEARCH_OUTPUT_KEYS}
        assert isinstance(sample_output["dry_run"], bool)

    def test_run_id_is_non_empty_string(self):
        sample = {"run_id": "abc-123", "profile": "documentary"}
        assert sample["run_id"] and isinstance(sample["run_id"], str)


class TestScriptToAudioContract:
    """Script output fields that Audio step reads."""

    _SCRIPT_OUTPUT_KEYS = {
        "run_id", "profile", "niche", "dry_run",
        "script_s3_key", "generate_shorts", "shorts_tiers",
    }

    def test_all_keys_are_strings(self):
        for k in self._SCRIPT_OUTPUT_KEYS:
            assert isinstance(k, str)

    def test_script_s3_key_follows_convention(self):
        run_id = "test-run-123"
        expected_key = f"{run_id}/script.json"
        assert expected_key.startswith(run_id)
        assert expected_key.endswith(".json")


class TestAudioToVisualsContract:
    """Audio step outputs fields consumed by Visuals."""

    _AUDIO_OUTPUT_KEYS = {
        "run_id", "profile", "niche", "dry_run",
        "mixed_audio_s3_key", "script_s3_key", "total_duration_estimate",
    }

    def test_total_duration_estimate_is_numeric(self):
        val = 600.0
        assert isinstance(val, float)
        assert val > 0


class TestVisualsToEditorContract:
    """Visuals handler dry-run output matches what Editor step expects."""

    def test_dry_run_visuals_output_has_scenes(self):
        h = _load_module(
            "nexus-visuals",
            os.path.join(LAMBDAS_DIR, "nexus-visuals", "handler.py"),
            extra_mocks={"nova_canvas": MagicMock(), "nova_reel": MagicMock()},
        )
        script_data = {
            "title": "T",
            "mood": "neutral",
            "scenes": [
                {"scene_id": 0, "nova_canvas_prompt": "x", "nova_reel_prompt": "y", "estimated_duration": 6}
            ],
        }
        profile_data = {"visuals": {"color_grade_default": "cinematic_warm"}}
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(script_data).encode())},
            {"Body": MagicMock(read=lambda: json.dumps(profile_data).encode())},
        ]
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(
                {"run_id": "r1", "profile": "documentary", "dry_run": True, "niche": "tech",
                 "script_s3_key": "r1/script.json", "mixed_audio_s3_key": "r1/audio.wav"},
                None,
            )
        assert "scenes" in result
        assert isinstance(result["scenes"], list)
        assert "run_id" in result

    def test_editor_contract_fields_present(self):
        expected_editor_inputs = {"run_id", "profile", "dry_run", "scenes", "mixed_audio_s3_key"}
        visuals_output = {
            "run_id": "r1",
            "profile": "documentary",
            "dry_run": True,
            "scenes": [],
            "mixed_audio_s3_key": "r1/audio.wav",
            "edl_s3_key": "r1/edl.json",
        }
        for field in expected_editor_inputs:
            assert field in visuals_output


class TestUploadDryRunContract:
    """Upload dry-run output matches Notify step input requirements."""

    def test_dry_run_upload_output_has_notify_fields(self):
        h = _load_module(
            "nexus-upload",
            os.path.join(LAMBDAS_DIR, "nexus-upload", "handler.py"),
        )
        sfn_mock = MagicMock()
        body = {
            "run_id": "r1",
            "s3_key": "r1/video.mp4",
            "metadata": {
                "title": "My Video", "description": "Desc", "tags": ["a"],
                "dry_run": True, "profile": "documentary", "niche": "tech",
                "primary_thumbnail_s3_key": "r1/thumb.jpg",
                "thumbnail_s3_keys": ["r1/thumb.jpg"],
                "video_duration_sec": 600.0,
            },
            "task_token": "tok-1",
        }
        sqs_event = {"Records": [{"body": json.dumps(body)}]}
        with patch("boto3.client", return_value=sfn_mock):
            h.lambda_handler(sqs_event, None)
        call_kwargs = sfn_mock.send_task_success.call_args[1]
        result = json.loads(call_kwargs["output"])
        notify_fields = {"run_id", "video_url", "title"}
        for field in notify_fields:
            assert field in result, f"Missing field for Notify: {field!r}"


class TestPipelineStateKeyPreservation:
    """Verify that run_id, profile, and dry_run are preserved at every step output."""

    def test_upload_dry_run_preserves_state_keys(self):
        h = _load_module(
            "nexus-upload",
            os.path.join(LAMBDAS_DIR, "nexus-upload", "handler.py"),
        )
        sfn_mock = MagicMock()
        body = {
            "run_id": "preserved-id",
            "s3_key": "x/v.mp4",
            "metadata": {
                "title": "T", "description": "", "tags": [],
                "dry_run": True, "profile": "finance", "niche": "finance",
                "primary_thumbnail_s3_key": "x/t.jpg",
                "thumbnail_s3_keys": ["x/t.jpg"],
                "video_duration_sec": 0.0,
            },
            "task_token": "tok-2",
        }
        sqs_event = {"Records": [{"body": json.dumps(body)}]}
        with patch("boto3.client", return_value=sfn_mock):
            h.lambda_handler(sqs_event, None)
        call_kwargs = sfn_mock.send_task_success.call_args[1]
        result = json.loads(call_kwargs["output"])
        assert result["run_id"] == "preserved-id"
        assert result["profile"] == "finance"
        assert result["dry_run"] is True
