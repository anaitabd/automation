"""Tests for nexus-brand-designer/handler.py — Claude brand kit generation."""

import json
import os
import sys
import importlib.util
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("CONFIG_BUCKET", "test-config")
os.environ.setdefault("BRAND_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

_MOD = None


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    handler_dir = os.path.join(LAMBDAS_DIR, "nexus-brand-designer")
    if handler_dir not in sys.path:
        sys.path.insert(0, handler_dir)
    spec = importlib.util.spec_from_file_location(
        "brand_designer_test", os.path.join(handler_dir, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brand_designer_test"] = mod
    with patch("boto3.client", return_value=MagicMock()):
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestParseBrandJson:
    def test_parses_markdown_wrapped_json(self):
        h = _load()
        text = '```json\n{"primary_color": "#FF0000", "font": "Impact"}\n```'
        result = h._parse_brand_json(text)
        assert result["primary_color"] == "#FF0000"

    def test_parses_bare_json(self):
        h = _load()
        text = '{"primary_color": "#00FF00"}'
        result = h._parse_brand_json(text)
        assert result["primary_color"] == "#00FF00"

    def test_parses_json_with_preamble(self):
        h = _load()
        text = 'Here is the brand kit:\n{"primary_color": "#0000FF"}\nHope this helps!'
        result = h._parse_brand_json(text)
        assert result["primary_color"] == "#0000FF"

    def test_raises_on_no_json(self):
        h = _load()
        with pytest.raises(ValueError, match="Could not parse"):
            h._parse_brand_json("No JSON here at all")


class TestLambdaHandler:
    def test_returns_brand_and_voice_id(self):
        h = _load()
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({
                "content": [{"type": "text", "text": json.dumps({
                    "primary_color": "#4F6EF7",
                    "secondary_color": "#1A1A2E",
                    "accent_color": "#FFD700",
                    "font": "Impact",
                    "lut_preset": "high_contrast",
                    "tagline": "Tech for all",
                    "thumbnail_style": "Bold and clean",
                    "brand_personality": ["smart", "bold", "fun"],
                })}]
            }).encode())
        }
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({
                "voice": {"voice_id": "test-voice"},
                "thumbnail": {"font": "Cinzel"},
                "shorts": {"lut_preset": "cinematic_teal_orange"},
            }).encode())
        }
        mock_s3.put_object.return_value = {}

        with patch("boto3.client") as mock_boto:
            def client_factory(service, **kwargs):
                if service == "bedrock-runtime":
                    return mock_bedrock
                return mock_s3
            mock_boto.side_effect = client_factory

            # Clear the module cache so profile reload works
            h._cache.clear()
            result = h.lambda_handler({
                "channel_id": "ch1",
                "channel_name": "TechHub",
                "niche": "technology",
                "profile": "documentary",
            }, None)

        assert "brand" in result
        assert "voice_id" in result
        assert result["brand"]["font"] in h.AVAILABLE_FONTS

    def test_falls_back_on_claude_error(self):
        h = _load()
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.side_effect = Exception("Claude down")
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({
                "voice": {"voice_id": "fallback-voice"},
                "thumbnail": {"font": "Cinzel"},
            }).encode())
        }
        mock_s3.put_object.return_value = {}

        with patch("boto3.client") as mock_boto:
            def client_factory(service, **kwargs):
                if service == "bedrock-runtime":
                    return mock_bedrock
                return mock_s3
            mock_boto.side_effect = client_factory

            h._cache.clear()
            result = h.lambda_handler({
                "channel_id": "ch1",
                "channel_name": "Test",
                "niche": "tech",
            }, None)

        assert result["brand"]["primary_color"] == "#4F6EF7"  # Default fallback


class TestColorValidation:
    def test_invalid_font_replaced_with_default(self):
        h = _load()
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({
                "content": [{"type": "text", "text": json.dumps({
                    "primary_color": "#FF0000",
                    "secondary_color": "#000000",
                    "accent_color": "#FFFF00",
                    "font": "INVALID_FONT_THAT_DOES_NOT_EXIST",
                    "lut_preset": "high_contrast",
                    "tagline": "Test",
                    "thumbnail_style": "Test",
                    "brand_personality": ["a", "b", "c"],
                })}]
            }).encode())
        }
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({
                "voice": {"voice_id": "v1"},
                "thumbnail": {"font": "Cinzel"},
            }).encode())
        }
        mock_s3.put_object.return_value = {}

        with patch("boto3.client") as mock_boto:
            def client_factory(service, **kwargs):
                if service == "bedrock-runtime":
                    return mock_bedrock
                return mock_s3
            mock_boto.side_effect = client_factory

            h._cache.clear()
            result = h.lambda_handler({
                "channel_id": "ch1",
                "channel_name": "Test",
                "niche": "tech",
            }, None)

        # Font should be the profile default, not the invalid one
        assert result["brand"]["font"] in h.AVAILABLE_FONTS

