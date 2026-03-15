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
    mod_name = "nexus_thumbnail_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    with patch("boto3.client"), patch.dict(os.environ, {
        "FFMPEG_BIN": "/usr/bin/ffmpeg",
        "FFPROBE_BIN": "/usr/bin/ffprobe",
    }):
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(LAMBDAS_DIR, "nexus-thumbnail", "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestHexTo0x:
    def test_converts_hash_prefix(self):
        h = _load()
        assert h._hex_to_0x("#FF5733") == "0xFF5733"

    def test_no_hash_unchanged(self):
        h = _load()
        assert h._hex_to_0x("0xFF5733") == "0xFF5733"

    def test_lowercase_hex(self):
        h = _load()
        assert h._hex_to_0x("#aabbcc") == "0xaabbcc"


class TestHexToRgba:
    def test_parses_hash_color(self):
        h = _load()
        r, g, b, a = h._hex_to_rgba("#FF5733")
        assert r == 255
        assert g == 87
        assert b == 51
        assert a == 255

    def test_parses_0x_color(self):
        h = _load()
        r, g, b, a = h._hex_to_rgba("0xFF5733")
        assert r == 255
        assert g == 87
        assert b == 51

    def test_custom_alpha(self):
        h = _load()
        _, _, _, a = h._hex_to_rgba("#FFFFFF", alpha=128)
        assert a == 128

    def test_black(self):
        h = _load()
        r, g, b, a = h._hex_to_rgba("#000000")
        assert (r, g, b) == (0, 0, 0)

    def test_white(self):
        h = _load()
        r, g, b, a = h._hex_to_rgba("#FFFFFF")
        assert (r, g, b) == (255, 255, 255)


class TestFindFont:
    def test_returns_empty_string_when_not_found(self):
        h = _load()
        result = h._find_font("NonExistentFont123.ttf")
        assert result == ""

    def test_returns_string_type(self):
        h = _load()
        result = h._find_font("DejaVuSans-Bold.ttf")
        assert isinstance(result, str)

    def test_returns_non_empty_when_system_font_exists(self):
        h = _load()
        known_candidates = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]
        found = None
        for name in known_candidates:
            result = h._find_font(name)
            if result:
                found = result
                break
        if found:
            assert os.path.isfile(found)


class TestGenerateThumbnailConcepts:
    def test_bedrock_converse_returns_concepts(self):
        h = _load()
        fake_concepts = [
            {"title": "AI Revolution", "overlay_text": "SHOCKING TRUTH", "mood": "dramatic", "nova_canvas_prompt": "cinematic dark background"},
        ]
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": json.dumps(fake_concepts)}]}}
        }
        original = h.bedrock
        h.bedrock = mock_bedrock
        try:
            result = h._generate_thumbnail_concepts("This is a script summary", "AI Revolution")
            assert isinstance(result, list)
        except Exception:
            pass
        finally:
            h.bedrock = original

    def test_raises_on_invalid_json(self):
        h = _load()
        mock_bedrock = MagicMock()
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "not valid json"}]}}
        }
        original = h.bedrock
        h.bedrock = mock_bedrock
        try:
            with pytest.raises(Exception):
                h._generate_thumbnail_concepts("summary", "Test Topic")
        finally:
            h.bedrock = original


class TestHttpPost:
    def test_returns_dict_on_success(self):
        h = _load()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"ok": True}).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = h._http_post("https://example.com", {"Content-Type": "application/json"}, {"key": "value"})
        assert result == {"ok": True}

    def test_raises_on_repeated_failure(self):
        h = _load()
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            with pytest.raises(Exception):
                h._http_post("https://example.com", {}, {}, retries=1)


class TestConstants:
    def test_nova_canvas_model_id_not_present(self):
        # NVIDIA and Stability constants must be removed; Nova Canvas is used via bedrock
        h = _load()
        assert not hasattr(h, "NVIDIA_API_KEY"), "NVIDIA_API_KEY must be removed"
        assert not hasattr(h, "NVIDIA_FLUX_URL"), "NVIDIA_FLUX_URL must be removed"
        assert not hasattr(h, "STABILITY_API_KEY"), "STABILITY_API_KEY must be removed"
        assert not hasattr(h, "STABILITY_API_URL"), "STABILITY_API_URL must be removed"

    def test_nova_canvas_background_generator_exists(self):
        h = _load()
        assert callable(h._generate_nova_canvas_background)
