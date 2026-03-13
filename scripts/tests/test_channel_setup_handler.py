"""Tests for nexus-channel-setup/handler.py — channel orchestration."""

import json
import os
import sys
import importlib.util
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")
os.environ.setdefault("BRAND_DESIGNER_FUNCTION", "nexus-brand-designer")
os.environ.setdefault("LOGO_GEN_FUNCTION", "nexus-logo-gen")
os.environ.setdefault("INTRO_OUTRO_FUNCTION", "nexus-intro-outro")

_MOD = None


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    handler_dir = os.path.join(LAMBDAS_DIR, "nexus-channel-setup")
    if handler_dir not in sys.path:
        sys.path.insert(0, handler_dir)
    spec = importlib.util.spec_from_file_location(
        "channel_setup_test", os.path.join(handler_dir, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["channel_setup_test"] = mod
    with patch("boto3.client", return_value=MagicMock()):
        with patch("psycopg2.connect", return_value=MagicMock()):
            spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestInvokeChain:
    def test_brand_designer_called_first(self):
        h = _load()
        call_order = []

        def mock_invoke(fn, payload):
            call_order.append(fn)
            if fn == h.BRAND_DESIGNER_FUNCTION:
                return {"brand": {"primary_color": "#123"}, "voice_id": "v1"}
            elif fn == h.LOGO_GEN_FUNCTION:
                return {"logo_s3_key": "channels/ch1/logo.png"}
            elif fn == h.INTRO_OUTRO_FUNCTION:
                return {"intro_s3_key": "channels/ch1/intro.mp4", "outro_s3_key": "channels/ch1/outro.mp4"}
            return {}

        with patch.object(h, "_invoke_lambda", side_effect=mock_invoke), \
             patch.object(h, "_update_channel"):
            result = h.lambda_handler({
                "channel_id": "ch1",
                "channel_name": "Test Channel",
                "niche": "tech",
            }, None)

        assert call_order[0] == h.BRAND_DESIGNER_FUNCTION
        assert call_order[1] == h.LOGO_GEN_FUNCTION
        assert result["status"] == "active"

    def test_intro_outro_failure_non_fatal(self):
        """FIX 2: intro-outro failure should not crash the pipeline."""
        h = _load()

        def mock_invoke(fn, payload):
            if fn == h.BRAND_DESIGNER_FUNCTION:
                return {"brand": {"primary_color": "#123"}, "voice_id": "v1"}
            elif fn == h.LOGO_GEN_FUNCTION:
                return {"logo_s3_key": "channels/ch1/logo.png"}
            elif fn == h.INTRO_OUTRO_FUNCTION:
                raise RuntimeError("nexus-intro-outro not implemented")
            return {}

        with patch.object(h, "_invoke_lambda", side_effect=mock_invoke), \
             patch.object(h, "_update_channel"):
            # Should NOT raise
            result = h.lambda_handler({
                "channel_id": "ch1",
                "channel_name": "Test Channel",
                "niche": "tech",
            }, None)

        assert result["status"] == "active"
        assert result["brand"].get("intro_s3") == ""
        assert result["brand"].get("outro_s3") == ""

    def test_brand_designer_failure_raises(self):
        """Brand designer is critical — failure should propagate."""
        h = _load()

        def mock_invoke(fn, payload):
            if fn == h.BRAND_DESIGNER_FUNCTION:
                raise RuntimeError("Claude error")
            return {}

        with patch.object(h, "_invoke_lambda", side_effect=mock_invoke), \
             patch.object(h, "_update_channel"), \
             patch.object(h, "_write_error"):
            with pytest.raises(RuntimeError, match="Claude error"):
                h.lambda_handler({
                    "channel_id": "ch1",
                    "channel_name": "Test",
                    "niche": "tech",
                }, None)


class TestWriteError:
    def test_write_error_does_not_raise(self):
        h = _load()
        s3_mock = MagicMock()
        s3_mock.put_object.side_effect = Exception("S3 down")
        with patch("boto3.client", return_value=s3_mock):
            # Should not raise
            h._write_error("ch1", "test-step", RuntimeError("test error"))

