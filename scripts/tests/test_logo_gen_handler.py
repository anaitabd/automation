import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("ASSETS_BUCKET", "test-assets")

_MOD = None


def _make_utils_mock():
    m = MagicMock()
    m.get_logger.return_value = MagicMock()
    return m


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    mod_name = "nexus_logo_gen_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    sys.modules["nova_canvas"] = MagicMock()
    sys.modules["shared"] = MagicMock()
    sys.modules["shared.nova_canvas"] = MagicMock()
    mock_gau = MagicMock(return_value="channels/ch1/logo.png")
    sys.modules["shared.nova_canvas"].generate_and_upload_image = mock_gau
    with patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(LAMBDAS_DIR, "nexus-logo-gen", "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestLambdaHandlerLogoGen:
    def _event(self, **overrides):
        base = {
            "channel_id": "ch-001",
            "channel_name": "Tech Insights",
            "niche": "technology",
            "profile": "documentary",
            "brand": {
                "primary_color": "#4F6EF7",
                "secondary_color": "#1A1A2E",
                "accent_color": "#FFD700",
                "font": "Cinzel",
            },
        }
        base.update(overrides)
        return base

    def test_returns_channel_id_and_logo_key(self):
        h = _load()
        mock_gen = MagicMock(return_value="channels/ch-001/logo.png")
        with patch.object(h, "generate_and_upload_image", mock_gen):
            result = h.lambda_handler(self._event(), None)
        assert result["channel_id"] == "ch-001"
        assert "logo_s3_key" in result

    def test_s3_key_includes_channel_id(self):
        h = _load()
        mock_gen = MagicMock(return_value="channels/ch-001/logo.png")
        with patch.object(h, "generate_and_upload_image", mock_gen):
            result = h.lambda_handler(self._event(), None)
        assert "ch-001" in result["logo_s3_key"]

    def test_falls_back_to_pillow_on_nova_failure(self):
        h = _load()
        mock_gen = MagicMock(side_effect=Exception("Nova Canvas failed"))
        fallback_mock = MagicMock(return_value="channels/ch-001/logo.png")
        with patch.object(h, "generate_and_upload_image", mock_gen), \
             patch.object(h, "_generate_fallback_logo", fallback_mock):
            result = h.lambda_handler(self._event(), None)
        fallback_mock.assert_called_once()
        assert result["logo_s3_key"] == "channels/ch-001/logo.png"

    def test_raises_when_both_nova_and_pillow_fail(self):
        h = _load()
        with patch.object(h, "generate_and_upload_image", side_effect=Exception("Nova error")), \
             patch.object(h, "_generate_fallback_logo", side_effect=Exception("Pillow error")):
            with pytest.raises(RuntimeError):
                h.lambda_handler(self._event(), None)

    def test_prompt_includes_channel_name(self):
        h = _load()
        captured = {}
        def fake_gen(prompt, **kwargs):
            captured["prompt"] = prompt
            return "channels/ch-001/logo.png"
        with patch.object(h, "generate_and_upload_image", side_effect=fake_gen):
            h.lambda_handler(self._event(), None)
        assert "Tech Insights" in captured.get("prompt", "")

    def test_prompt_includes_niche(self):
        h = _load()
        captured = {}
        def fake_gen(prompt, **kwargs):
            captured["prompt"] = prompt
            return "channels/ch-001/logo.png"
        with patch.object(h, "generate_and_upload_image", side_effect=fake_gen):
            h.lambda_handler(self._event(), None)
        assert "technology" in captured.get("prompt", "")


class TestInitialsExtraction:
    def test_two_word_name_gives_two_initials(self):
        h = _load()
        mock_boto = MagicMock()
        mock_boto.put_object.return_value = {}
        try:
            from PIL import Image, ImageDraw, ImageFont
            with patch("boto3.client", return_value=mock_boto):
                key = h._generate_fallback_logo("Tech Insights", {}, "ch/logo.png")
            assert key == "ch/logo.png"
        except ImportError:
            pytest.skip("Pillow not available")

    def test_single_word_name_uses_first_two_chars(self):
        h = _load()
        mock_boto = MagicMock()
        mock_boto.put_object.return_value = {}
        try:
            from PIL import Image, ImageDraw, ImageFont
            with patch("boto3.client", return_value=mock_boto):
                key = h._generate_fallback_logo("Tech", {}, "ch/logo.png")
            assert key == "ch/logo.png"
        except ImportError:
            pytest.skip("Pillow not available")
