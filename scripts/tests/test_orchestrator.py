"""Tests for scripts/orchestrator.py — pipeline definitions, parallel groups, ETA."""

import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

_MOD = None


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    spec = importlib.util.spec_from_file_location(
        "orchestrator_test", os.path.join(SCRIPTS_DIR, "orchestrator.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_test"] = mod
    with patch("requests.post", return_value=MagicMock(status_code=200, json=lambda: {})):
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestPipelineDefinition:
    def test_all_nine_steps_defined(self):
        o = _load()
        names = [s["name"] for s in o.PIPELINE]
        assert len(names) == 9
        assert names == ["Research", "Script", "Audio", "Visuals", "Editor", "Shorts", "Thumbnail", "Upload", "Notify"]

    def test_each_step_has_required_keys(self):
        o = _load()
        for step in o.PIPELINE:
            assert "name" in step
            assert "input_keys" in step
            assert "merge_keys" in step
            assert isinstance(step["input_keys"], list)
            assert isinstance(step["merge_keys"], list)

    def test_upload_step_exists(self):
        """FIX 6: Upload step must exist in orchestrator pipeline."""
        o = _load()
        names = [s["name"] for s in o.PIPELINE]
        assert "Upload" in names
        upload_idx = names.index("Upload")
        thumb_idx = names.index("Thumbnail")
        notify_idx = names.index("Notify")
        assert thumb_idx < upload_idx < notify_idx

    def test_shorts_step_is_optional(self):
        o = _load()
        shorts_step = next(s for s in o.PIPELINE if s["name"] == "Shorts")
        assert shorts_step.get("optional") is True


class TestParallelGroups:
    def test_audio_visuals_parallel(self):
        o = _load()
        assert "Audio" in o._PARALLEL_GROUP
        assert "Visuals" in o._PARALLEL_GROUP

    def test_editor_shorts_parallel(self):
        o = _load()
        assert "Editor" in o._PARALLEL_CONTENT_GROUP
        assert "Shorts" in o._PARALLEL_CONTENT_GROUP

    def test_no_overlap_between_groups(self):
        o = _load()
        assert o._PARALLEL_GROUP & o._PARALLEL_CONTENT_GROUP == set()


class TestEndpoints:
    def test_docker_endpoints_defined(self):
        o = _load()
        # In docker mode, all 9 steps should have endpoints
        if o.MODE == "docker":
            for step in o.PIPELINE:
                assert step["name"] in o._ENDPOINTS

    def test_all_step_names_have_endpoints(self):
        o = _load()
        for step in o.PIPELINE:
            assert step["name"] in o._ENDPOINTS, f"Missing endpoint for {step['name']}"


class TestStepTimeouts:
    def test_heavy_steps_have_extended_timeouts(self):
        o = _load()
        assert o._STEP_TIMEOUTS.get("Visuals", 0) >= 900
        assert o._STEP_TIMEOUTS.get("Editor", 0) >= 900
        assert o._STEP_TIMEOUTS.get("Shorts", 0) >= 900


class TestEstimateDuration:
    def test_returns_default_for_unknown_step(self):
        o = _load()
        est = o._estimate_step_duration("Research")
        assert est is not None
        assert est > 0

    def test_returns_none_for_totally_unknown(self):
        o = _load()
        est = o._estimate_step_duration("NonExistentStep")
        assert est is None


class TestCreateRun:
    def test_creates_run_dict(self):
        o = _load()
        run = o._create_run("test-id", "tech", "documentary", False)
        assert run["run_id"] == "test-id"
        assert run["niche"] == "tech"
        assert run["status"] == "RUNNING"
        assert len(run["steps"]) == len(o.PIPELINE)

    def test_all_steps_start_pending(self):
        o = _load()
        run = o._create_run("test-id", "tech", "documentary", False)
        for step in run["steps"]:
            assert step["status"] == "pending"

    def test_eta_calculated(self):
        o = _load()
        run = o._create_run("test-id", "tech", "documentary", False)
        assert run["eta_remaining_sec"] is not None
        assert run["eta_remaining_sec"] > 0

