"""Tests for the JSON repair logic in nexus-script handler.

Run with:  python -m pytest test_repair.py -v
"""
import json
import sys
import os

# Make the handler importable without boto3 / AWS deps by stubbing them
import types

# Stub boto3 so the handler module loads without AWS credentials
_fake_boto3 = types.ModuleType("boto3")
_fake_boto3.client = lambda *a, **kw: None  # type: ignore
sys.modules["boto3"] = _fake_boto3

# Now import the functions under test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lambdas", "nexus-script"))
from handler import _repair_truncated_json, _extract_json


# ── Helpers ──────────────────────────────────────────────────────────────────

VALID_SCRIPT = {
    "title": "How to Get Started Investing in the Stock Market for Beginners",
    "description": "Learn the basics of investing in stocks as a beginner.",
    "tags": ["investing", "stock market", "beginners"],
    "hook": "What if I told you that you could start investing with just $10?",
    "hook_emotion": "curious",
    "sections": [
        {
            "title": "Introduction",
            "content": "Welcome to this guide. [PAUSE] Today we cover the basics.",
            "emotion": "neutral",
            "duration_estimate_sec": 45,
            "visual_cue": {
                "search_queries": ["stock market trading floor", "investment charts", "beginner investor"],
                "camera_style": "ken_burns_in",
                "color_grade": "clean_corporate",
                "transition_in": "dissolve",
                "overlay_type": "lower_third",
                "overlay_text": "Getting Started with Investing"
            }
        },
        {
            "title": "Understanding Stocks",
            "content": "A stock represents ownership in a company. [BEAT] When you buy a share...",
            "emotion": "confident",
            "duration_estimate_sec": 120,
            "visual_cue": {
                "search_queries": ["stock certificates", "wall street", "company shares"],
                "camera_style": "pan_right",
                "color_grade": "clean_corporate",
                "transition_in": "crossfade",
                "overlay_type": "stat_counter",
                "overlay_text": "Over 58% of Americans own stocks"
            }
        }
    ],
    "cta": "Subscribe for more investing tips!",
    "total_duration_estimate": 600,
    "mood": "educational_confident"
}


def _truncate_at(s: str, pos: int) -> str:
    """Truncate a JSON string at the given character position."""
    return s[:pos]


# ── Test: valid JSON passes through ──────────────────────────────────────────

def test_valid_json_passthrough():
    raw = json.dumps(VALID_SCRIPT, indent=2)
    result = _extract_json(raw)
    assert result["title"] == VALID_SCRIPT["title"]
    assert len(result["sections"]) == 2


def test_valid_json_with_markdown_fence():
    raw = "```json\n" + json.dumps(VALID_SCRIPT, indent=2) + "\n```"
    result = _extract_json(raw)
    assert result["title"] == VALID_SCRIPT["title"]


def test_valid_json_with_preamble():
    raw = "Here is the script:\n\n" + json.dumps(VALID_SCRIPT, indent=2)
    result = _extract_json(raw)
    assert result["title"] == VALID_SCRIPT["title"]


# ── Test: truncation mid-string ──────────────────────────────────────────────

def test_truncated_mid_string_value():
    raw = json.dumps(VALID_SCRIPT, indent=2)
    # Truncate in the middle of the "description" value
    cut = raw.index("basics of investing") + 10
    truncated = raw[:cut]
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert "title" in result  # title comes before the truncation point


def test_truncated_mid_key():
    raw = json.dumps(VALID_SCRIPT, indent=2)
    # Truncate in the middle of the "hook_emotion" key
    cut = raw.index('"hook_emo') + 6
    truncated = raw[:cut]
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert "title" in result
    # hook was completed before hook_emotion, so it should survive
    assert "hook" in result


# ── Test: truncation after comma ─────────────────────────────────────────────

def test_truncated_after_trailing_comma():
    raw = json.dumps(VALID_SCRIPT, indent=2)
    # Find the comma after "description" value and truncate right after it
    desc_end = raw.index("Learn the basics") + len("Learn the basics of investing in stocks as a beginner.\"")
    # Move to the next comma
    next_comma = raw.index(",", desc_end)
    truncated = raw[:next_comma + 1]
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert "title" in result


# ── Test: truncation inside sections array (the main bug scenario) ───────────

def test_truncated_mid_second_section():
    """Simulate the exact failure: truncation inside the 2nd section object."""
    raw = json.dumps(VALID_SCRIPT, indent=2)
    # Find the second section's "content" and truncate mid-value
    second_content = raw.index("A stock represents ownership")
    cut = second_content + 20
    truncated = raw[:cut]
    result = _extract_json(truncated)
    assert "title" in result
    # Should have at least the first complete section
    assert len(result.get("sections", [])) >= 1


def test_truncated_inside_visual_cue():
    """Truncation inside a nested visual_cue object."""
    raw = json.dumps(VALID_SCRIPT, indent=2)
    idx = raw.index("stock market trading floor") + 15
    truncated = raw[:idx]
    result = _extract_json(truncated)
    assert "title" in result


# ── Test: truncation at various structural points ────────────────────────────

def test_truncated_after_colon_no_value():
    truncated = '{"title": "Test", "description":'
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert result["title"] == "Test"


def test_truncated_boolean():
    truncated = '{"title": "Test", "active": tru'
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert result["title"] == "Test"


def test_truncated_null():
    truncated = '{"title": "Test", "extra": nul'
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert result["title"] == "Test"


def test_truncated_number_with_dot():
    truncated = '{"title": "Test", "value": 3.'
    result = _extract_json(truncated)
    assert isinstance(result, dict)
    assert result["title"] == "Test"


def test_truncated_array_mid_element():
    truncated = '{"title": "Test", "tags": ["investing", "stoc'
    result = _extract_json(truncated)
    assert result["title"] == "Test"


# ── Test: the _repair_truncated_json directly ────────────────────────────────

def test_repair_closes_brackets():
    fragment = '{"a": [1, 2, 3'
    result = _repair_truncated_json(fragment)
    assert result["a"] == [1, 2, 3]


def test_repair_closes_nested():
    fragment = '{"a": {"b": {"c": "val"'
    result = _repair_truncated_json(fragment)
    assert isinstance(result, dict)
    assert "a" in result
    # The nested structure should be recovered
    assert isinstance(result["a"], dict)


def test_repair_strips_trailing_comma():
    fragment = '{"a": 1, "b": 2,'
    result = _repair_truncated_json(fragment)
    assert result == {"a": 1, "b": 2}


def test_repair_partial_object_in_array():
    fragment = '{"items": [{"id": 1, "name": "ok"}, {"id": 2, "nam'
    result = _repair_truncated_json(fragment)
    assert "items" in result
    # Should keep at least the first complete item
    assert len(result["items"]) >= 1


# ── Test: realistic large truncation ─────────────────────────────────────────

def test_realistic_large_script_truncation():
    """Build a script with many sections and truncate like the real error."""
    script = dict(VALID_SCRIPT)
    # Duplicate sections to make it large
    script["sections"] = script["sections"] * 5
    raw = json.dumps(script, indent=2)
    # Truncate at ~60% through
    cut = int(len(raw) * 0.6)
    truncated = raw[:cut]
    result = _extract_json(truncated)
    assert "title" in result
    assert len(result.get("sections", [])) >= 1
    print(f"  Recovered {len(result.get('sections', []))} of 10 sections from truncation at char {cut}/{len(raw)}")


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed:
        sys.exit(1)






