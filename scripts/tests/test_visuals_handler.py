import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")
SHARED_DIR = os.path.join(LAMBDAS_DIR, "shared")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")

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
    mod_name = "nexus_visuals_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    sys.modules["nova_canvas"] = MagicMock()
    sys.modules["nova_reel"] = MagicMock()
    with patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(LAMBDAS_DIR, "nexus-visuals", "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestEnvironmentDefaults:
    def test_default_duration_sec(self):
        h = _load()
        assert h.NOVA_REEL_DURATION_SEC == int(os.environ.get("NOVA_REEL_DURATION_SEC", "6"))

    def test_default_fps(self):
        h = _load()
        assert h.NOVA_REEL_FPS == int(os.environ.get("NOVA_REEL_FPS", "24"))

    def test_default_resolution_hd(self):
        h = _load()
        assert h.NOVA_REEL_WIDTH == int(os.environ.get("NOVA_REEL_WIDTH", "1280"))
        assert h.NOVA_REEL_HEIGHT == int(os.environ.get("NOVA_REEL_HEIGHT", "720"))


class TestDryRunPath:
    def _make_event(self, scenes=None):
        return {
            "run_id": "test-run",
            "profile": "documentary",
            "niche": "technology",
            "script_s3_key": "test-run/script.json",
            "dry_run": True,
            "mixed_audio_s3_key": "test-run/audio.wav",
            "total_duration_estimate": 600.0,
        }

    def test_dry_run_returns_without_calling_nova(self):
        h = _load()
        script_data = {
            "title": "Test Video",
            "mood": "neutral",
            "scenes": [
                {"scene_id": 1, "nova_canvas_prompt": "a city", "nova_reel_prompt": "zoom in", "estimated_duration": 6},
            ],
        }
        profile_data = {"visuals": {"color_grade_default": "cinematic_warm"}}
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(script_data).encode())},
            {"Body": MagicMock(read=lambda: json.dumps(profile_data).encode())},
        ]
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(self._make_event(), None)
        assert result["dry_run"] is True
        assert result["run_id"] == "test-run"
        assert len(result["scenes"]) == 1
        assert "dry_run" in result["scenes"][0]["clip_s3_key"]

    def test_dry_run_preserves_scene_ids(self):
        h = _load()
        scenes = [
            {"scene_id": i, "nova_canvas_prompt": f"scene {i}", "nova_reel_prompt": f"reel {i}", "estimated_duration": 6}
            for i in range(3)
        ]
        script_data = {"title": "T", "mood": "neutral", "scenes": scenes}
        profile_data = {"visuals": {"color_grade_default": "cinematic_warm"}}
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(script_data).encode())},
            {"Body": MagicMock(read=lambda: json.dumps(profile_data).encode())},
        ]
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(self._make_event(), None)
        scene_ids = [s["scene_id"] for s in result["scenes"]]
        assert scene_ids == [0, 1, 2]

    def test_dry_run_keys_contain_run_id(self):
        h = _load()
        script_data = {
            "title": "T",
            "scenes": [{"scene_id": 0, "nova_canvas_prompt": "x", "nova_reel_prompt": "y", "estimated_duration": 6}],
        }
        profile_data = {"visuals": {"color_grade_default": "cinematic_warm"}}
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(script_data).encode())},
            {"Body": MagicMock(read=lambda: json.dumps(profile_data).encode())},
        ]
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler({"run_id": "my-run", "profile": "documentary", "dry_run": True, "niche": "t"}, None)
        for scene in result["scenes"]:
            assert "my-run" in scene["clip_s3_key"]


class TestWriteError:
    def test_write_error_does_not_raise(self):
        h = _load()
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.put_object.side_effect = Exception("S3 error")
            h._write_error("run-1", "visuals", ValueError("test error"))


class TestRekognitionScore:
    def test_happy_path(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {
            "Labels": [
                {"Name": "City", "Confidence": 99},
                {"Name": "Building", "Confidence": 95},
                {"Name": "Sky", "Confidence": 88},
            ]
        }
        with patch.object(h, "rekognition", mock_rek):
            score = h._rekognition_score(b"fakejpeg", "city building")
        assert score == 1.0

    def test_no_matching_labels(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {
            "Labels": [{"Name": "Ocean", "Confidence": 99}]
        }
        with patch.object(h, "rekognition", mock_rek):
            score = h._rekognition_score(b"fakejpeg", "city building")
        assert score == 0.0

    def test_partial_match(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {
            "Labels": [{"Name": "City", "Confidence": 99}, {"Name": "Ocean", "Confidence": 80}]
        }
        with patch.object(h, "rekognition", mock_rek):
            score = h._rekognition_score(b"fakejpeg", "city building")
        assert score == pytest.approx(0.5)


class TestClaudeVisionScore:
    def test_happy_path(self):
        h = _load()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "0.85"}]}}
        }
        with patch.object(h, "bedrock", mock_bedrock):
            score = h._claude_vision_score(b"fakejpeg", "city skyline at night")
        assert score == pytest.approx(0.85)

    def test_strips_whitespace(self):
        h = _load()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "  0.72\n"}]}}
        }
        with patch.object(h, "bedrock", mock_bedrock):
            score = h._claude_vision_score(b"fakejpeg", "forest")
        assert score == pytest.approx(0.72)


class TestSelectBestCandidate:
    def test_returns_none_for_empty_candidates(self):
        h = _load()
        result = h._select_best_candidate([], "city skyline")
        assert result is None

    def test_single_candidate_returns_bytes(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {"Labels": [{"Name": "City", "Confidence": 99}]}
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "0.9"}]}}
        }
        with patch.object(h, "rekognition", mock_rek), patch.object(h, "bedrock", mock_bedrock):
            result = h._select_best_candidate([(b"img1", "id1")], "city")
        assert result == b"img1"

    def test_best_rekognition_candidate_goes_to_claude(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.side_effect = [
            {"Labels": [{"Name": "City", "Confidence": 99}]},
            {"Labels": []},
            {"Labels": []},
            {"Labels": []},
            {"Labels": []},
        ]
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "0.95"}]}}
        }
        candidates = [(b"img1", "id1"), (b"img2", "id2"), (b"img3", "id3"), (b"img4", "id4"), (b"img5", "id5")]
        with patch.object(h, "rekognition", mock_rek), patch.object(h, "bedrock", mock_bedrock):
            result = h._select_best_candidate(candidates, "city")
        assert result == b"img1"

    def test_rekognition_failure_falls_back_to_zero_score(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.side_effect = Exception("Rekognition error")
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "0.5"}]}}
        }
        candidates = [(b"img1", "id1"), (b"img2", "id2")]
        with patch.object(h, "rekognition", mock_rek), patch.object(h, "bedrock", mock_bedrock):
            result = h._select_best_candidate(candidates, "city")
        assert result in (b"img1", b"img2")

    def test_claude_failure_falls_back_to_zero_score(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.side_effect = [
            {"Labels": [{"Name": "City", "Confidence": 99}]},
            {"Labels": [{"Name": "Forest", "Confidence": 99}]},
        ]
        mock_bedrock = MagicMock()
        mock_bedrock.converse.side_effect = Exception("Bedrock error")
        candidates = [(b"img1", "id1"), (b"img2", "id2")]
        with patch.object(h, "rekognition", mock_rek), patch.object(h, "bedrock", mock_bedrock):
            result = h._select_best_candidate(candidates, "city")
        assert result in (b"img1", b"img2")

    def test_top3_only_sent_to_claude(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {"Labels": []}
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "0.5"}]}}
        }
        candidates = [(f"img{i}".encode(), f"id{i}") for i in range(5)]
        with patch.object(h, "rekognition", mock_rek), patch.object(h, "bedrock", mock_bedrock):
            h._select_best_candidate(candidates, "city")
        assert mock_bedrock.converse.call_count == 3


class TestGetSecret:
    def test_caches_secret(self):
        h = _load()
        h._cache.clear() if hasattr(h, "_cache") else None
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": "test-key"})
        }
        with patch("boto3.client", return_value=mock_sm):
            h._cache.clear() if hasattr(h, "_cache") else None
            result = h.get_secret("test/secret")
        assert result == {"api_key": "test-key"}


class TestRekognitionScoreTrueCrime:
    def test_boost_dark_labels(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {
            "Labels": [
                {"Name": "Night", "Confidence": 99},
                {"Name": "Shadow", "Confidence": 90},
                {"Name": "Forest", "Confidence": 85},
            ]
        }
        profile = {"script": {"style": "true_crime"}}
        with patch.object(h, "rekognition", mock_rek):
            score = h._rekognition_score(b"fakejpeg", "night shadow", profile=profile)
        # Boost from matching dark labels should push score up
        assert score > 0.0

    def test_penalize_bright_labels(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {
            "Labels": [
                {"Name": "Beach", "Confidence": 99},
                {"Name": "Sunshine", "Confidence": 90},
                {"Name": "Party", "Confidence": 80},
            ]
        }
        profile = {"script": {"style": "true_crime"}}
        with patch.object(h, "rekognition", mock_rek):
            score_with_profile = h._rekognition_score(
                b"fakejpeg", "beach sunshine party", profile=profile
            )
            score_no_profile = h._rekognition_score(
                b"fakejpeg", "beach sunshine party", profile=None
            )
        # Penalties should reduce the score for a true crime profile
        assert score_with_profile < score_no_profile

    def test_no_profile_unchanged_behavior(self):
        h = _load()
        mock_rek = MagicMock()
        mock_rek.detect_labels.return_value = {
            "Labels": [{"Name": "City", "Confidence": 99}]
        }
        with patch.object(h, "rekognition", mock_rek):
            score = h._rekognition_score(b"fakejpeg", "city", profile=None)
        assert score == 1.0


class TestAvoidNovaReel:
    def test_avoid_nova_reel_flag_respected_in_dry_run(self):
        h = _load()
        profile_data = {
            "script": {"style": "true_crime"},
            "visuals": {
                "color_grade_default": "dark_cinematic",
                "avoid_nova_reel": True,
                "pexels_keywords": ["dark street night", "crime scene"],
            }
        }
        script_data = {
            "title": "The Murder Case",
            "scenes": [{"scene_id": 1, "nova_canvas_prompt": "dark alley", "estimated_duration": 6}],
        }
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = [
            {"Body": MagicMock(read=lambda: json.dumps(script_data).encode())},
            {"Body": MagicMock(read=lambda: json.dumps(profile_data).encode())},
        ]
        with patch("boto3.client", return_value=s3_mock):
            result = h.lambda_handler(
                {"run_id": "tc-run", "profile": "true_crime", "dry_run": True,
                 "niche": "true_crime", "script_s3_key": "tc-run/script.json"},
                None
            )
        assert result["dry_run"] is True
        assert "tc-run" in result["scenes"][0]["clip_s3_key"]

    def test_pexels_video_returns_none_on_missing_key(self):
        h = _load()
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": ""})
        }
        with patch("boto3.client", return_value=mock_sm):
            h._cache.clear()
            result = h._fetch_pexels_video("dark street", tmpdir="/tmp", scene_id=0)
        assert result is None

    def test_pexels_photo_returns_none_on_missing_key(self):
        h = _load()
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": ""})
        }
        with patch("boto3.client", return_value=mock_sm):
            h._cache.clear()
            result = h._fetch_pexels_photo("dark street", tmpdir="/tmp", scene_id=0)
        assert result is None
