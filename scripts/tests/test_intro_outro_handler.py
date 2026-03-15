"""Tests for lambdas/nexus-intro-outro/handler.py.

All AWS and subprocess calls are mocked — no live AWS calls made.
"""

import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch, call

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")

_MOD = None


def _make_xray_mock():
    """Create a minimal aws_xray_sdk mock with in_subsegment context manager."""
    xray_core = MagicMock()

    @contextmanager
    def _in_subsegment(name):
        yield

    xray_core.xray_recorder.in_subsegment = _in_subsegment
    xray_core.patch_all = MagicMock()
    return xray_core


def _make_utils_mock():
    m = MagicMock()
    m.get_logger.return_value = MagicMock()
    m.notify_step_start = MagicMock()
    m.notify_step_complete = MagicMock()
    return m


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD

    mod_name = "nexus_intro_outro_handler_test"
    for k in list(sys.modules.keys()):
        if "intro_outro" in k:
            del sys.modules[k]

    # Mock external dependencies before import
    xray_mock = _make_xray_mock()
    sys.modules["aws_xray_sdk"] = MagicMock()
    sys.modules["aws_xray_sdk.core"] = xray_mock

    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()

    with patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            mod_name,
            os.path.join(LAMBDAS_DIR, "nexus-intro-outro", "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    _MOD = mod
    return mod


def _event(**overrides):
    base = {
        "run_id": "run-test-001",
        "profile": "documentary",
        "channel_id": "ch-001",
        "dry_run": False,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
class TestDryRun:
    def test_dry_run_skips_generation(self):
        """dry_run=True must not call subprocess.run and must return None keys."""
        h = _load()
        with patch("subprocess.run") as mock_sub, \
             patch.object(h, "s3", MagicMock()):
            result = h.handler(_event(dry_run=True), None)

        mock_sub.assert_not_called()
        assert result["intro_s3_key"] is None
        assert result["outro_s3_key"] is None


@pytest.mark.unit
class TestLogoMissing:
    def test_logo_missing_uses_none_gracefully(self):
        """Handler continues without logo when S3 download fails."""
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = Exception("NoSuchKey")
        mock_s3.get_object.side_effect = Exception("NotFound")
        mock_s3.upload_file.return_value = None

        mock_sub = MagicMock()
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stderr = ""

        with patch.object(h, "s3", mock_s3), \
             patch("subprocess.run", mock_sub):
            result = h.handler(_event(), None)

        # Should not raise; keys may be set or None depending on subprocess mocking
        assert "intro_s3_key" in result
        assert "outro_s3_key" in result


@pytest.mark.unit
class TestBrandFallback:
    def test_brand_fallback_to_profile_defaults(self):
        """When both brand.json and profile.json are missing, defaults are used."""
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NotFound")

        brand = h._load_brand("documentary", "ch-001")

        assert brand["primary_color"] == "E8593C"
        assert brand["channel_name"] == "Documentary"

    def test_brand_loaded_from_profile_json(self):
        """When brand.json missing but profile.json present, use profile data."""
        h = _load()
        mock_s3 = MagicMock()

        def get_object(Bucket, Key):
            if "brand.json" in Key:
                raise Exception("NotFound")
            # Return profile.json
            body = MagicMock()
            body.read.return_value = json.dumps({
                "channel_name": "Finance Hub",
                "brand": {"primary_color": "#1a2b3c"},
                "channel_cta": "New videos every Monday",
            }).encode()
            return {"Body": body}

        mock_s3.get_object.side_effect = get_object

        with patch.object(h, "s3", mock_s3):
            brand = h._load_brand("finance", "ch-001")

        assert brand["channel_name"] == "Finance Hub"
        assert brand["primary_color"] == "1a2b3c"
        assert brand["channel_cta"] == "New videos every Monday"


@pytest.mark.unit
class TestIntroUpload:
    def test_intro_built_and_uploaded(self):
        """Successful run uploads intro.mp4 to the correct S3 key."""
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = Exception("NoLogo")
        mock_s3.get_object.side_effect = Exception("NoBrand")
        mock_s3.upload_file.return_value = None

        mock_sub = MagicMock()
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stderr = ""

        with patch.object(h, "s3", mock_s3), \
             patch("subprocess.run", mock_sub):
            result = h.handler(_event(run_id="run-abc"), None)

        # upload_file(local_path, bucket, key) — key is the 3rd positional arg
        uploaded_calls = mock_s3.upload_file.call_args_list
        uploaded_keys = [c[0][2] for c in uploaded_calls]
        assert "run-abc/editor/intro.mp4" in uploaded_keys
        assert result["intro_s3_key"] == "run-abc/editor/intro.mp4"


@pytest.mark.unit
class TestOutroUpload:
    def test_outro_built_and_uploaded(self):
        """Successful run uploads outro.mp4 to the correct S3 key."""
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = Exception("NoLogo")
        mock_s3.get_object.side_effect = Exception("NoBrand")
        mock_s3.upload_file.return_value = None

        mock_sub = MagicMock()
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stderr = ""

        with patch.object(h, "s3", mock_s3), \
             patch("subprocess.run", mock_sub):
            result = h.handler(_event(run_id="run-xyz"), None)

        # upload_file(local_path, bucket, key) — key is the 3rd positional arg
        uploaded_calls = mock_s3.upload_file.call_args_list
        uploaded_keys = [c[0][2] for c in uploaded_calls]
        assert "run-xyz/editor/outro.mp4" in uploaded_keys
        assert result["outro_s3_key"] == "run-xyz/editor/outro.mp4"


@pytest.mark.unit
class TestFFmpegFailureNonFatal:
    def test_ffmpeg_failure_is_nonfatal(self):
        """FFmpeg failure returns None keys and does not raise."""
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = Exception("NoLogo")
        mock_s3.get_object.side_effect = Exception("NoBrand")

        mock_sub = MagicMock()
        mock_sub.return_value.returncode = 1
        mock_sub.return_value.stderr = "FFmpeg error: invalid filter"

        with patch.object(h, "s3", mock_s3), \
             patch("subprocess.run", mock_sub):
            result = h.handler(_event(), None)

        assert result["intro_s3_key"] is None
        assert result["outro_s3_key"] is None


@pytest.mark.unit
class TestXRaySubsegments:
    def test_xray_subsegments_called(self):
        """X-Ray subsegments intro-render, outro-render, and s3-upload are entered."""
        h = _load()
        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = Exception("NoLogo")
        mock_s3.get_object.side_effect = Exception("NoBrand")
        mock_s3.upload_file.return_value = None

        mock_sub = MagicMock()
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stderr = ""

        entered_segments = []

        @contextmanager
        def tracking_subsegment(name):
            entered_segments.append(name)
            yield

        # Patch xray_recorder on the handler module (bound at import time)
        with patch.object(h.xray_recorder, "in_subsegment", tracking_subsegment), \
             patch.object(h, "s3", mock_s3), \
             patch("subprocess.run", mock_sub):
            h.handler(_event(), None)

        assert "intro-render" in entered_segments
        assert "outro-render" in entered_segments
        assert "s3-upload" in entered_segments
