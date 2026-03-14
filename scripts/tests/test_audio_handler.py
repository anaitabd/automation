import importlib.util
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")
os.environ.setdefault("FFMPEG_BIN", "/usr/bin/ffmpeg")

_MOD = None


def _make_utils_mock():
    m = MagicMock()
    m.get_logger.return_value = MagicMock()
    m.notify_step_start.return_value = 0.0
    m.notify_step_complete.return_value = None
    return m


def _load():
    global _MOD
    if _MOD is not None:
        return _MOD
    mod_name = "nexus_audio_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    with patch("boto3.client"), patch.dict(os.environ, {"FFMPEG_BIN": "/usr/bin/ffmpeg"}):
        spec = importlib.util.spec_from_file_location(
            mod_name, os.path.join(LAMBDAS_DIR, "nexus-audio", "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    _MOD = mod
    return mod


class TestCleanText:
    def test_replaces_pause_marker(self):
        h = _load()
        assert h._clean_text("[PAUSE] hello") == "... hello"

    def test_replaces_beat_marker(self):
        h = _load()
        assert h._clean_text("hello[BEAT]world") == "hello,world"

    def test_replaces_breath_marker(self):
        h = _load()
        result = h._clean_text("hi[BREATH]there")
        assert "..." in result

    def test_strips_whitespace(self):
        h = _load()
        assert h._clean_text("  hello  ") == "hello"

    def test_no_markers_unchanged(self):
        h = _load()
        assert h._clean_text("plain text") == "plain text"

    def test_multiple_markers(self):
        h = _load()
        result = h._clean_text("[PAUSE] intro [BEAT] mid [BREATH] end")
        assert "[PAUSE]" not in result
        assert "[BEAT]" not in result
        assert "[BREATH]" not in result


class TestDetectEmotion:
    def test_detects_tense_keyword(self):
        h = _load()
        assert h._detect_emotion("This is a crisis situation") == "tense"

    def test_detects_dramatic_keyword(self):
        h = _load()
        assert h._detect_emotion("Secret exposed to the world") == "dramatic"

    def test_detects_somber_keyword(self):
        h = _load()
        assert h._detect_emotion("A tragedy occurred today") == "somber"

    def test_detects_excited_keyword(self):
        h = _load()
        assert h._detect_emotion("An incredible breakthrough was made") == "excited"

    def test_detects_confident_keyword(self):
        h = _load()
        assert h._detect_emotion("Data shows that results are in") == "confident"

    def test_defaults_to_neutral(self):
        h = _load()
        assert h._detect_emotion("The sun rose this morning") == "neutral"

    def test_custom_default_emotion(self):
        h = _load()
        assert h._detect_emotion("Nothing special here", "calm") == "calm"

    def test_case_insensitive(self):
        h = _load()
        assert h._detect_emotion("This is a CRISIS situation") == "tense"


class TestGetVoiceSettings:
    def _profile(self, **overrides):
        base = {
            "voice": {
                "stability": 0.4,
                "similarity_boost": 0.8,
                "style": 0.5,
                "emotion_mapping": {},
            }
        }
        base["voice"].update(overrides)
        return base

    def test_returns_base_settings(self):
        h = _load()
        settings = h._get_voice_settings(self._profile(), "neutral")
        assert settings["stability"] == 0.4
        assert settings["similarity_boost"] == 0.8
        assert settings["use_speaker_boost"] is True

    def test_emotion_mapping_overrides_stability(self):
        h = _load()
        profile = self._profile(emotion_mapping={"tense": {"stability": 0.9, "style": 0.1}})
        settings = h._get_voice_settings(profile, "tense")
        assert settings["stability"] == 0.9
        assert settings["style"] == 0.1

    def test_emotion_not_in_mapping_uses_defaults(self):
        h = _load()
        profile = self._profile(emotion_mapping={"excited": {"stability": 0.2}})
        settings = h._get_voice_settings(profile, "neutral")
        assert settings["stability"] == 0.4

    def test_missing_voice_key_uses_defaults(self):
        h = _load()
        settings = h._get_voice_settings({}, "neutral")
        assert "stability" in settings
        assert "similarity_boost" in settings
        assert settings["use_speaker_boost"] is True


class TestFetchPixabayMusic:
    def test_returns_none_when_no_api_key(self):
        h = _load()
        result = h._fetch_pixabay_music("energetic_hype", "", "/tmp")
        assert result is None

    def test_returns_none_on_empty_hits(self):
        h = _load()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"hits": []}).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = h._fetch_pixabay_music("energetic_hype", "test-key", "/tmp")
        assert result is None

    def test_returns_none_on_http_error(self):
        h = _load()
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            result = h._fetch_pixabay_music("energetic_hype", "test-key", "/tmp")
        assert result is None

    def test_mood_keyword_mapped_to_search_term(self):
        h = _load()
        captured_url = []
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"hits": []}).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            captured_url.append(req.full_url if hasattr(req, "full_url") else str(req))
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            h._fetch_pixabay_music("corporate_upbeat_subtle", "key", "/tmp")
        assert len(captured_url) > 0
        assert "corporate" in captured_url[0].lower() or "pixabay" in captured_url[0].lower()


class TestMusicMoodKeywords:
    def test_all_expected_moods_defined(self):
        h = _load()
        assert "tension_atmospheric" in h.MUSIC_MOOD_KEYWORDS
        assert "corporate_upbeat_subtle" in h.MUSIC_MOOD_KEYWORDS
        assert "energetic_hype" in h.MUSIC_MOOD_KEYWORDS

    def test_mood_values_are_strings(self):
        h = _load()
        for k, v in h.MUSIC_MOOD_KEYWORDS.items():
            assert isinstance(v, str), f"Mood value for {k!r} is not a string"


class TestPacingMap:
    def test_all_markers_present(self):
        h = _load()
        assert "[PAUSE]" in h.PACING_MAP
        assert "[BEAT]" in h.PACING_MAP
        assert "[BREATH]" in h.PACING_MAP


class TestPollyFallback:
    def test_quota_exceeded_falls_back_to_polly(self):
        h = _load()
        error_body = json.dumps(
            {
                "detail": {
                    "status": "quota_exceeded",
                    "message": "No credits remaining",
                }
            }
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="https://api.elevenlabs.io/v1/text-to-speech/test-voice",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(error_body),
        )
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"polly-bytes")}

        with patch.object(h, "_synthesize_sentence", side_effect=http_error), \
             patch("boto3.client", return_value=polly):
            result = h._synthesize_sentence_with_fallback(
                "hello world",
                "test-voice",
                {},
                "test-key",
                "documentary",
                {"voice": {}},
            )

        assert result == b"polly-bytes"
        polly.synthesize_speech.assert_called_once()

    def test_non_auth_error_does_not_fallback(self):
        h = _load()
        with patch.object(h, "_synthesize_sentence", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                h._synthesize_sentence_with_fallback(
                    "hello world",
                    "test-voice",
                    {},
                    "test-key",
                    "documentary",
                    {"voice": {}},
                )


class TestPollyFallbackExtended:
    def _make_http_error(self, code: int, body: dict | None = None) -> urllib.error.HTTPError:
        import io
        raw = json.dumps(body or {}).encode("utf-8")
        return urllib.error.HTTPError(
            url="https://api.elevenlabs.io/v1/text-to-speech/test-voice",
            code=code,
            msg="Error",
            hdrs=None,
            fp=io.BytesIO(raw),
        )

    def test_elevenlabs_429_triggers_polly_fallback(self):
        h = _load()
        http_error = self._make_http_error(429)
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"polly-bytes")}

        with patch.object(h, "_synthesize_sentence", side_effect=http_error), \
             patch("boto3.client", return_value=polly):
            result = h._synthesize_sentence_with_fallback(
                "hello world",
                "test-voice",
                {},
                "test-key",
                "documentary",
                {"voice": {}},
            )

        assert result == b"polly-bytes"
        polly.synthesize_speech.assert_called_once()

    def test_elevenlabs_401_triggers_polly_fallback(self):
        h = _load()
        http_error = self._make_http_error(401)
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"polly-bytes-401")}

        with patch.object(h, "_synthesize_sentence", side_effect=http_error), \
             patch("boto3.client", return_value=polly):
            result = h._synthesize_sentence_with_fallback(
                "hello world",
                "test-voice",
                {},
                "test-key",
                "documentary",
                {"voice": {}},
            )

        assert result == b"polly-bytes-401"
        polly.synthesize_speech.assert_called_once()

    def test_polly_neural_ssml_emotion_mapping(self):
        h = _load()
        expected = {
            "tense":         {"rate": "slow",   "pitch": "-2st"},
            "excited":       {"rate": "fast",   "pitch": "+3st"},
            "reflective":    {"rate": "x-slow", "pitch": "-3st"},
            "authoritative": {"rate": "medium", "pitch": "-1st"},
            "somber":        {"rate": "slow",   "pitch": "-4st"},
            "hopeful":       {"rate": "medium", "pitch": "+1st"},
            "neutral":       {"rate": "medium", "pitch": "0st"},
        }
        for emotion, attrs in expected.items():
            ssml = h._build_ssml("test text", emotion)
            assert f'rate="{attrs["rate"]}"' in ssml, f"{emotion}: rate mismatch"
            assert f'pitch="{attrs["pitch"]}"' in ssml, f"{emotion}: pitch mismatch"
            assert "test text" in ssml
            assert '<amazon:effect name="drc">' in ssml

    def test_polly_standard_called_when_neural_fails(self):
        h = _load()
        http_error = self._make_http_error(429)

        polly_neural = MagicMock()
        polly_neural.synthesize_speech.side_effect = RuntimeError("neural unavailable")

        polly_standard = MagicMock()
        polly_standard.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"standard-bytes")}

        polly_call_count = [0]

        def fake_boto3_client(service):
            if service == "polly":
                polly_call_count[0] += 1
                return polly_neural if polly_call_count[0] == 1 else polly_standard
            return MagicMock()

        with patch.object(h, "_synthesize_sentence", side_effect=http_error), \
             patch("boto3.client", side_effect=fake_boto3_client):
            result = h._synthesize_sentence_with_fallback(
                "hello world",
                "test-voice",
                {},
                "test-key",
                "documentary",
                {"voice": {}},
            )

        assert result == b"standard-bytes"
        polly_standard.synthesize_speech.assert_called_once()
        called_kwargs = polly_standard.synthesize_speech.call_args[1]
        assert called_kwargs.get("Engine") == "standard"
        assert called_kwargs.get("TextType") == "text"
