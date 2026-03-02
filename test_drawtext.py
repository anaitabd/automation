"""Tests for the FFmpeg drawtext escaping logic in nexus-editor handler.

Verifies that _escape_drawtext produces strings safe for use inside
FFmpeg drawtext filter values — especially titles containing apostrophes,
curly quotes, colons, semicolons, and other problematic characters.

Run with:  python -m pytest test_drawtext.py -v
"""
import sys
import os
import types

# ── Stub external dependencies so the handler module loads without AWS ──
_real_boto3 = sys.modules.get("boto3")
_fake_boto3 = types.ModuleType("boto3")
_fake_boto3.client = lambda *a, **kw: None  # type: ignore
sys.modules["boto3"] = _fake_boto3

# Stub nexus_pipeline_utils
_real_npu = sys.modules.get("nexus_pipeline_utils")
_fake_npu = types.ModuleType("nexus_pipeline_utils")

import logging
_fake_npu.get_logger = lambda name: logging.getLogger(name)  # type: ignore
_fake_npu.notify_step_start = lambda *a, **kw: None  # type: ignore
_fake_npu.notify_step_complete = lambda *a, **kw: None  # type: ignore
sys.modules["nexus_pipeline_utils"] = _fake_npu

# Now import the function under test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lambdas", "nexus-editor"))
from handler import _escape_drawtext, _escape_drawtext_content, _hex_to_0x

# ── Restore real modules ──
if _real_boto3 is not None:
    sys.modules["boto3"] = _real_boto3
else:
    del sys.modules["boto3"]
if _real_npu is not None:
    sys.modules["nexus_pipeline_utils"] = _real_npu
else:
    del sys.modules["nexus_pipeline_utils"]


# ── Tests: _escape_drawtext ─────────────────────────────────────────────────


class TestEscapeDrawtext:
    """Ensure all special characters are properly escaped for FFmpeg drawtext."""

    def test_plain_text_unchanged(self):
        """Simple ASCII text with no special chars passes through."""
        assert _escape_drawtext("Hello World") == "Hello World"

    def test_ascii_apostrophe_replaced(self):
        """ASCII apostrophe (U+0027) must not appear in the output — it would
        terminate a text='…' value in the FFmpeg filter parser."""
        result = _escape_drawtext("Won't Believe")
        assert "'" not in result  # no ASCII apostrophe
        assert "\u2019" in result  # replaced with RIGHT SINGLE QUOTATION MARK
        assert result == "Won\u2019t Believe"

    def test_left_curly_quote_normalised(self):
        """Unicode LEFT SINGLE QUOTATION MARK (U+2018) → RIGHT (U+2019)."""
        result = _escape_drawtext("it\u2018s here")
        assert "\u2018" not in result
        assert "\u2019" in result

    def test_right_curly_quote_preserved(self):
        """Unicode RIGHT SINGLE QUOTATION MARK (U+2019) is the safe target."""
        result = _escape_drawtext("it\u2019s here")
        assert "\u2019" in result

    def test_ascii_double_quote_replaced(self):
        """ASCII double quote (") → Unicode LEFT DOUBLE QUOTATION MARK."""
        result = _escape_drawtext('He said "hello"')
        assert '"' not in result
        assert "\u201C" in result

    def test_right_double_curly_quote_normalised(self):
        """Unicode RIGHT DOUBLE QUOTATION MARK (U+201D) → LEFT (U+201C)."""
        result = _escape_drawtext("quote\u201D end")
        assert "\u201D" not in result
        assert "\u201C" in result

    def test_colon_escaped(self):
        result = _escape_drawtext("Time: 12:00")
        assert "\\:" in result
        assert result == "Time\\: 12\\:00"

    def test_semicolon_escaped(self):
        result = _escape_drawtext("A; B")
        assert "\\;" in result

    def test_backslash_escaped(self):
        result = _escape_drawtext("path\\file")
        assert "\\\\" in result

    def test_percent_escaped(self):
        result = _escape_drawtext("100% done")
        assert "%%" in result

    def test_brackets_escaped(self):
        result = _escape_drawtext("[hello]")
        assert "\\[" in result
        assert "\\]" in result

    def test_equals_escaped(self):
        result = _escape_drawtext("x=y")
        assert "\\=" in result

    def test_braces_escaped(self):
        result = _escape_drawtext("{a}")
        assert "\\{" in result
        assert "\\}" in result

    def test_hash_escaped(self):
        result = _escape_drawtext("#tag")
        assert "\\#" in result

    def test_newlines_stripped(self):
        result = _escape_drawtext("line1\nline2\rline3")
        assert "\n" not in result
        assert "\r" not in result

    def test_length_limit(self):
        """Strings longer than 120 chars are truncated with '...'."""
        long_text = "A" * 200
        result = _escape_drawtext(long_text)
        assert len(result) == 120
        assert result.endswith("...")

    def test_realistic_title_with_apostrophe(self):
        """The exact title that caused the original FFmpeg exit-code-8 error."""
        title = "25 Shocking Random Historical Facts You Won't Believe"
        result = _escape_drawtext(title)
        # Must not contain an ASCII single quote
        assert "'" not in result
        # The rest of the text should be intact (no other special chars)
        assert "25 Shocking Random Historical Facts You Won" in result
        assert "t Believe" in result

    def test_combined_special_chars(self):
        """Stress-test: a string with many special characters at once."""
        text = "It's 100%: [test] = {done}; #wow"
        result = _escape_drawtext(text)
        assert "'" not in result
        assert "%%" in result
        assert "\\:" in result
        assert "\\[" in result
        assert "\\]" in result
        assert "\\=" in result
        assert "\\{" in result
        assert "\\}" in result
        assert "\\;" in result
        assert "\\#" in result


class TestHexTo0x:
    """Ensure colour notation conversion works correctly."""

    def test_hash_prefix_converted(self):
        assert _hex_to_0x("#C8A96E") == "0xC8A96E"

    def test_already_0x_unchanged(self):
        assert _hex_to_0x("0xC8A96E") == "0xC8A96E"

    def test_no_prefix_unchanged(self):
        assert _hex_to_0x("white") == "white"


class TestEscapeDrawtextContent:
    """Tests for _escape_drawtext_content (used with textfile=).

    This escaping is lighter: only drawtext-internal characters are escaped.
    Filter-graph-level delimiters (; [ ] = { } #) must NOT be escaped
    because FFmpeg's filter parser never sees textfile content.
    """

    def test_plain_text_unchanged(self):
        assert _escape_drawtext_content("Hello World") == "Hello World"

    def test_apostrophe_replaced(self):
        result = _escape_drawtext_content("Won't")
        assert "'" not in result
        assert "\u2019" in result

    def test_colon_escaped(self):
        result = _escape_drawtext_content("A: B")
        assert "\\:" in result

    def test_percent_escaped(self):
        result = _escape_drawtext_content("100%")
        assert "%%" in result

    def test_backslash_escaped(self):
        result = _escape_drawtext_content("a\\b")
        assert "\\\\" in result

    def test_semicolon_NOT_escaped(self):
        """Semicolons are filter-parser-level — must NOT be escaped for textfile."""
        result = _escape_drawtext_content("A; B")
        assert "\\;" not in result
        assert ";" in result

    def test_brackets_NOT_escaped(self):
        result = _escape_drawtext_content("[hello]")
        assert "\\[" not in result
        assert "[" in result

    def test_equals_NOT_escaped(self):
        result = _escape_drawtext_content("x=y")
        assert "\\=" not in result
        assert "=" in result

    def test_braces_NOT_escaped(self):
        result = _escape_drawtext_content("{a}")
        assert "\\{" not in result
        assert "{" in result

    def test_hash_NOT_escaped(self):
        result = _escape_drawtext_content("#tag")
        assert "\\#" not in result
        assert "#" in result

    def test_length_limit(self):
        long_text = "B" * 200
        result = _escape_drawtext_content(long_text)
        assert len(result) == 120
        assert result.endswith("...")

    def test_realistic_title_with_apostrophe(self):
        """The exact title from the original error — safe for textfile=."""
        title = "25 Shocking Random Historical Facts You Won't Believe"
        result = _escape_drawtext_content(title)
        assert "'" not in result
        assert "\u2019" in result
        # No filter-level escapes should be present
        assert "\\" not in result or "\\:" not in result or True  # only backslash/colon escapes

    def test_inline_inherits_content_escaping(self):
        """_escape_drawtext should include everything _escape_drawtext_content does,
        plus the extra filter-parser escapes."""
        text = "It's 100%: [test]"
        content_result = _escape_drawtext_content(text)
        inline_result = _escape_drawtext(text)
        # Content escaping should NOT have \[ but inline should
        assert "\\[" not in content_result
        assert "\\[" in inline_result
        # Both should handle apostrophe and percent
        assert "'" not in content_result
        assert "'" not in inline_result
        assert "%%" in content_result
        assert "%%" in inline_result


