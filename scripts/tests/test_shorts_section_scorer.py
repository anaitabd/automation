"""Tests for nexus-shorts/section_scorer.py — section scoring and selection."""

import os
import sys
import importlib.util
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SHORTS_DIR = os.path.join(REPO_ROOT, "lambdas", "nexus-shorts")


def _load_scorer():
    if SHORTS_DIR not in sys.path:
        sys.path.insert(0, SHORTS_DIR)
    spec = importlib.util.spec_from_file_location(
        "section_scorer_test", os.path.join(SHORTS_DIR, "section_scorer.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch("boto3.client", return_value=MagicMock()):
        spec.loader.exec_module(mod)
    return mod


class TestScoreSection:
    def test_score_returns_0_to_100(self):
        scorer = _load_scorer()
        section = {
            "title": "Test Section",
            "narration_text": "This is a very interesting topic about technology and innovation.",
            "visual_cue": {"camera_style": "tracking", "overlay_type": "text"},
            "hook": "Did you know that AI is changing everything?",
        }
        score = scorer.score_section(section, "documentary")
        assert 0 <= score <= 100

    def test_empty_section_scores_low(self):
        scorer = _load_scorer()
        section = {"title": "", "narration_text": "", "visual_cue": {}}
        score = scorer.score_section(section, "documentary")
        assert score < 50

    def test_rich_section_scores_higher(self):
        scorer = _load_scorer()
        poor = {"title": "x", "narration_text": "short", "visual_cue": {}}
        rich = {
            "title": "The Revolutionary Discovery That Changed Everything",
            "narration_text": "In 2024, scientists made a shocking discovery that completely "
                         "transformed our understanding of the universe. This hidden secret "
                         "was never meant to be found. " * 3,
            "visual_cue": {"camera_style": "tracking", "overlay_type": "text"},
            "nova_reel_prompt": "cinematic shot",
            "duration_estimate_sec": 15,
        }
        poor_score = scorer.score_section(poor, "documentary")
        rich_score = scorer.score_section(rich, "documentary")
        assert rich_score > poor_score


class TestSelectSections:
    def test_returns_correct_count(self):
        scorer = _load_scorer()
        sections = [
            {"title": f"Section {i}", "narration_text": f"Content for section {i}. " * 10, "visual_cue": {}}
            for i in range(10)
        ]
        selected = scorer.select_sections(sections, "documentary", count=3)
        assert len(selected) == 3

    def test_returns_all_when_count_exceeds(self):
        scorer = _load_scorer()
        sections = [{"title": "A", "narration_text": "Content", "visual_cue": {}}]
        selected = scorer.select_sections(sections, "documentary", count=5)
        assert len(selected) <= 5

    def test_distributes_across_thirds(self):
        scorer = _load_scorer()
        sections = [
            {"title": f"Section {i}", "narration_text": f"Content {i}. " * 20, "visual_cue": {}}
            for i in range(9)
        ]
        selected = scorer.select_sections(sections, "documentary", count=3)
        assert len(selected) == 3

    def test_empty_sections_returns_empty(self):
        scorer = _load_scorer()
        selected = scorer.select_sections([], "documentary", count=3)
        assert len(selected) == 0

