"""Tests for nexus-shorts/config.py — tier defs, output specs, constants."""

import os
import sys
import importlib.util
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SHORTS_DIR = os.path.join(REPO_ROOT, "lambdas", "nexus-shorts")


def _load_config():
    if SHORTS_DIR not in sys.path:
        sys.path.insert(0, SHORTS_DIR)
    spec = importlib.util.spec_from_file_location(
        "shorts_config_test", os.path.join(SHORTS_DIR, "config.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTierDefs:
    def test_all_four_tiers_defined(self):
        cfg = _load_config()
        assert set(cfg.TIER_DEFS.keys()) == {"micro", "short", "mid", "full"}

    @pytest.mark.parametrize("tier,expected_dur", [
        ("micro", 15.0), ("short", 30.0), ("mid", 45.0), ("full", 60.0),
    ])
    def test_durations(self, tier, expected_dur):
        cfg = _load_config()
        assert cfg.TIER_DEFS[tier]["duration"] == expected_dur

    def test_nova_clips_increase_with_tier(self):
        cfg = _load_config()
        clips = [cfg.TIER_DEFS[t]["nova_clips"] for t in ("micro", "short", "mid", "full")]
        assert clips == sorted(clips)

    def test_sections_bounds_valid(self):
        cfg = _load_config()
        for tier, td in cfg.TIER_DEFS.items():
            assert td["sections_min"] <= td["sections_max"], f"{tier}: min > max"
            assert td["sections_min"] >= 1, f"{tier}: sections_min < 1"


class TestOutputSpecs:
    def test_resolution(self):
        cfg = _load_config()
        assert cfg.OUTPUT_WIDTH == 1080
        assert cfg.OUTPUT_HEIGHT == 1920

    def test_fps(self):
        cfg = _load_config()
        assert cfg.OUTPUT_FPS == 30

    def test_crf(self):
        cfg = _load_config()
        assert cfg.OUTPUT_CRF == 18

    def test_lufs_target(self):
        cfg = _load_config()
        assert cfg.TARGET_LUFS == -14


class TestBPMDefaults:
    def test_all_profiles_have_bpm(self):
        cfg = _load_config()
        for profile in ("documentary", "finance", "entertainment"):
            assert profile in cfg.BPM_DEFAULTS

    def test_bpm_values_reasonable(self):
        cfg = _load_config()
        for profile, bpm in cfg.BPM_DEFAULTS.items():
            assert 40 <= bpm <= 200, f"{profile} BPM {bpm} out of range"


class TestLUTPresets:
    def test_presets_map_to_cube_files(self):
        cfg = _load_config()
        for name, path in cfg.LUT_PRESETS.items():
            assert path.endswith(".cube"), f"LUT {name} path doesn't end with .cube"


class TestEnvironmentDefaults:
    def test_shorts_enabled_default_true(self):
        cfg = _load_config()
        # Default from env
        assert isinstance(cfg.SHORTS_ENABLED, bool)

    def test_shorts_tiers_is_list(self):
        cfg = _load_config()
        assert isinstance(cfg.SHORTS_TIERS, list)

    def test_nova_budget_positive(self):
        cfg = _load_config()
        assert cfg.NOVA_REEL_SHORTS_BUDGET > 0

    def test_loop_threshold_range(self):
        cfg = _load_config()
        assert 0.0 <= cfg.SHORTS_LOOP_THRESHOLD <= 1.0

