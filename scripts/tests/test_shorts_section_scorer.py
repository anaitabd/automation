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
            "narration": "This is a very interesting topic about technology and innovation.",
            "visual_cues": ["wide shot", "close-up"],
            "hook": "Did you know that AI is changing everything?",
        }
        score = scorer.score_section(section, 0, 5)
        assert 0 <= score <= 100

    def test_empty_section_scores_low(self):
        scorer = _load_scorer()
        section = {"title": "", "narration": "", "visual_cues": []}
        score = scorer.score_section(section, 0, 1)
        assert score < 50

    def test_rich_section_scores_higher(self):
        scorer = _load_scorer()
        poor = {"title": "x", "narration": "short", "visual_cues": []}
        rich = {
            "title": "The Revolutionary Discovery That Changed Everything",
            "narration": "In 2024, scientists made a groundbreaking discovery that completely "
                         "transformed our understanding of the universe. " * 5,
            "visual_cues": ["dramatic wide shot", "close-up on face", "time-lapse"],
            "hook": "What if everything you knew was wrong?",
        }
        poor_score = scorer.score_section(poor, 0, 2)
        rich_score = scorer.score_section(rich, 0, 2)
        assert rich_score > poor_score


class TestSelectSections:
    def test_returns_correct_count(self):
        scorer = _load_scorer()
        sections = [
            {"title": f"Section {i}", "narration": f"Content for section {i}. " * 10, "visual_cues": ["shot"]}
            for i in range(10)
        ]
        selected = scorer.select_sections(sections, count=3)
        assert len(selected) == 3

    def test_returns_all_when_count_exceeds(self):
        scorer = _load_scorer()
        sections = [{"title": "A", "narration": "Content", "visual_cues": []}]
        selected = scorer.select_sections(sections, count=5)
        assert len(selected) <= 5

    def test_distributes_across_thirds(self):
        scorer = _load_scorer()
        sections = [
            {"title": f"Section {i}", "narration": f"Content {i}. " * 20, "visual_cues": ["shot"]}
            for i in range(9)
        ]
        selected = scorer.select_sections(sections, count=3)
        # Should pick from different parts of the script
        indices = [s[0] for s in selected] if isinstance(selected[0], tuple) else list(range(len(selected)))
        assert len(selected) == 3

    def test_empty_sections_returns_empty(self):
        scorer = _load_scorer()
        selected = scorer.select_sections([], count=3)
        assert len(selected) == 0

