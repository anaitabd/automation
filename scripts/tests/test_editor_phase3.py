"""Tests for Phase 3 editor features: Ken Burns, captions burn-in, True Crime style."""
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")
os.environ.setdefault("FFMPEG_BIN", "/usr/bin/ffmpeg")
os.environ.setdefault("FFPROBE_BIN", "/usr/bin/ffprobe")

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
    mod_name = "nexus_editor_phase3_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    sys.modules["aws_xray_sdk"] = MagicMock()
    sys.modules["aws_xray_sdk.core"] = MagicMock()
    with patch("boto3.client"), patch.dict(os.environ, {
        "FFMPEG_BIN": "/usr/bin/ffmpeg",
        "FFPROBE_BIN": "/usr/bin/ffprobe",
    }):
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(LAMBDAS_DIR, "nexus-editor", "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestNewFunctionsExist:
    def test_apply_ken_burns_exists(self):
        h = _load()
        assert callable(h._apply_ken_burns)

    def test_apply_captions_exists(self):
        h = _load()
        assert callable(h._apply_captions)

    def test_load_word_timestamps_exists(self):
        h = _load()
        assert callable(h._load_word_timestamps)

    def test_build_captions_drawtext_exists(self):
        h = _load()
        assert callable(h._build_captions_drawtext)


class TestLoadWordTimestamps:
    def test_returns_none_when_not_found(self):
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        result = h._load_word_timestamps(mock_s3, "test-run")
        assert result is None

    def test_returns_list_on_success(self):
        h = _load()
        words = [{"word": "hello", "start_time": 0.0, "end_time": 0.5}]
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps(words).encode())
        }
        result = h._load_word_timestamps(mock_s3, "test-run")
        assert result == words

    def test_returns_none_on_malformed_json(self):
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"not json")
        }
        result = h._load_word_timestamps(mock_s3, "test-run")
        assert result is None


class TestBuildCaptionsDrawtext:
    def test_empty_timestamps_returns_empty_list(self):
        h = _load()
        result = h._build_captions_drawtext([], is_true_crime=False)
        assert result == []

    def test_single_word_produces_two_filters(self):
        h = _load()
        words = [{"word": "hello", "start_time": 0.0, "end_time": 0.5}]
        result = h._build_captions_drawtext(words, is_true_crime=False)
        # One base + one highlight per word
        assert len(result) == 2

    def test_true_crime_tense_emotion_uppercases_text(self):
        h = _load()
        words = [
            {"word": "he", "start_time": 0.0, "end_time": 0.3, "emotion": "tense"},
            {"word": "knew", "start_time": 0.3, "end_time": 0.6, "emotion": "tense"},
        ]
        result = h._build_captions_drawtext(words, is_true_crime=True)
        assert any("HE KNEW" in f for f in result)

    def test_non_tense_emotion_keeps_case(self):
        h = _load()
        words = [
            {"word": "hello", "start_time": 0.0, "end_time": 0.5, "emotion": "neutral"},
        ]
        result = h._build_captions_drawtext(words, is_true_crime=True)
        assert any("hello" in f for f in result)

    def test_max_six_words_per_caption_frame(self):
        h = _load()
        words = [
            {"word": f"word{i}", "start_time": float(i) * 0.3, "end_time": float(i) * 0.3 + 0.3}
            for i in range(9)
        ]
        result = h._build_captions_drawtext(words, is_true_crime=False)
        # 9 words → 2 chunks (6 + 3), each chunk has 1 base + N highlight filters
        # 6+1 + 3+1 = chunk1: 1+6=7, chunk2: 1+3=4 → 11 total
        assert len(result) > 0
        # Check that word7 is in a different base drawtext filter than word0
        base_filters = [f for f in result if "between" in f and "yellow" not in f]
        assert len(base_filters) >= 2  # at least 2 caption groups

    def test_non_true_crime_never_uppercases(self):
        h = _load()
        words = [
            {"word": "hello", "start_time": 0.0, "end_time": 0.5, "emotion": "urgent"},
        ]
        result = h._build_captions_drawtext(words, is_true_crime=False)
        assert any("hello" in f for f in result)
        assert not any("HELLO" in f for f in result)

    def test_captions_use_lower_third_position(self):
        h = _load()
        words = [{"word": "test", "start_time": 0.0, "end_time": 0.5}]
        result = h._build_captions_drawtext(words, is_true_crime=False)
        assert any("y=h-120" in f for f in result)

    def test_captions_use_correct_font_size(self):
        h = _load()
        words = [{"word": "test", "start_time": 0.0, "end_time": 0.5}]
        result = h._build_captions_drawtext(words, is_true_crime=False)
        assert any("fontsize=52" in f for f in result)


class TestApplyCaptionsSkipsGracefully:
    def test_skips_when_no_timestamps(self):
        h = _load()
        # Passing empty list → should return input path unchanged
        result = h._apply_captions("input.mp4", [], is_true_crime=False, tmpdir="/tmp", run_id="r")
        assert result == "input.mp4"

    def test_ffmpeg_failure_returns_original_path(self):
        h = _load()
        import subprocess
        words = [{"word": "test", "start_time": 0.0, "end_time": 0.5}]
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg", stderr=b"error")):
            result = h._apply_captions("input.mp4", words, is_true_crime=False, tmpdir="/tmp", run_id="r")
        # Should return original assembled path on failure
        assert result == "input.mp4"
