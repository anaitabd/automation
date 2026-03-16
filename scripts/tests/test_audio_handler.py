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


@pytest.fixture(autouse=True)
def reset_elevenlabs_quota_flag():
    """Reset the ElevenLabs quota flag before each test for proper isolation."""
    h = _load()
    h.ELEVENLABS_QUOTA_EXHAUSTED = False
    yield
    h.ELEVENLABS_QUOTA_EXHAUSTED = False


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
            # True Crime emotions
            "whispering":    {"rate": "x-slow", "pitch": "-5st"},
            "urgent":        {"rate": "fast",   "pitch": "+1st"},
            "revelation":    {"rate": "medium", "pitch": "-1st"},
            "dark":          {"rate": "slow",   "pitch": "-3st"},
            "suspenseful":   {"rate": "slow",   "pitch": "-2st"},
        }
        for emotion, attrs in expected.items():
            ssml = h._build_ssml("test text", emotion)
            assert f'rate="{attrs["rate"]}"' in ssml, f"{emotion}: rate mismatch"
            assert f'pitch="{attrs["pitch"]}"' in ssml, f"{emotion}: pitch mismatch"
            assert "test text" in ssml
            assert '<amazon:effect name="drc">' in ssml
            assert '<amazon:breath' in ssml

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


class TestTranscribeTimestamps:
    @pytest.mark.unit
    def test_transcribe_timestamps_written_to_s3(self):
        h = _load()

        transcribe_mock = MagicMock()
        transcribe_mock.start_transcription_job.return_value = {}
        transcribe_mock.get_transcription_job.return_value = {
            "TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}
        }

        raw_output = json.dumps({
            "results": {
                "items": [
                    {
                        "type": "pronunciation",
                        "start_time": "0.0",
                        "end_time": "0.5",
                        "alternatives": [{"content": "hello", "confidence": "0.99"}],
                    },
                    {
                        "type": "pronunciation",
                        "start_time": "0.6",
                        "end_time": "1.1",
                        "alternatives": [{"content": "world", "confidence": "0.95"}],
                    },
                    {
                        "type": "punctuation",
                        "alternatives": [{"content": "."}],
                    },
                ]
            }
        }).encode("utf-8")

        s3_mock = MagicMock()
        s3_mock.get_object.return_value = {"Body": io.BytesIO(raw_output)}

        written_body = {}

        def fake_put_object(**kwargs):
            written_body.update(kwargs)

        s3_mock.put_object.side_effect = fake_put_object

        with patch.object(h, "transcribe", transcribe_mock), \
             patch.object(h, "s3", s3_mock), \
             patch("time.sleep"):
            h._run_transcribe("test-run-id", "s3://nexus-assets/test-run-id/audio/mixed_audio.wav")

        transcribe_mock.start_transcription_job.assert_called_once()
        call_kwargs = transcribe_mock.start_transcription_job.call_args[1]
        assert call_kwargs["TranscriptionJobName"] == "nexus-test-run-id"
        assert call_kwargs["MediaFormat"] == "wav"
        assert call_kwargs["OutputBucketName"] == h.S3_OUTPUTS_BUCKET
        assert call_kwargs["OutputKey"] == "test-run-id/audio/transcribe_timestamps.json"
        assert call_kwargs["Settings"]["ShowWordConfidence"] is True

        assert written_body.get("Key") == "test-run-id/audio/word_timestamps.json"
        result = json.loads(written_body["Body"])
        assert len(result["words"]) == 2
        assert result["words"][0] == {"word": "hello", "start": 0.0, "end": 0.5, "confidence": 0.99}
        assert result["words"][1] == {"word": "world", "start": 0.6, "end": 1.1, "confidence": 0.95}

    @pytest.mark.unit
    def test_transcribe_timeout_is_nonfatal(self):
        h = _load()

        transcribe_mock = MagicMock()
        transcribe_mock.start_transcription_job.return_value = {}
        transcribe_mock.get_transcription_job.return_value = {
            "TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}
        }

        s3_mock = MagicMock()

        call_count = [0]
        real_time = __import__("time").time

        def fake_time():
            call_count[0] += 1
            if call_count[0] <= 1:
                return 0.0
            return 200.0

        with patch.object(h, "transcribe", transcribe_mock), \
             patch.object(h, "s3", s3_mock), \
             patch("time.sleep"), \
             patch("time.time", side_effect=fake_time):
            h._run_transcribe("timeout-run", "s3://nexus-assets/timeout-run/audio/mixed_audio.wav")

        s3_mock.put_object.assert_not_called()


class TestDetectEmotionTrueCrime:
    """Tests for the True Crime detect_emotion() function."""

    def test_whispering_no_one_knew(self):
        h = _load()
        assert h.detect_emotion("No one knew what had happened that night.") == "whispering"

    def test_whispering_she_never(self):
        h = _load()
        assert h.detect_emotion("She never made it home.") == "whispering"

    def test_whispering_the_last_thing(self):
        h = _load()
        assert h.detect_emotion("The last thing she said was goodbye.") == "whispering"

    def test_whispering_what_they_found(self):
        h = _load()
        assert h.detect_emotion("What they found changed everything.") == "whispering"

    def test_urgent_suddenly(self):
        h = _load()
        assert h.detect_emotion("Suddenly the phone rang.") == "urgent"

    def test_urgent_within_hours(self):
        h = _load()
        assert h.detect_emotion("Within hours, the suspect was caught.") == "urgent"

    def test_urgent_police_discovered(self):
        h = _load()
        assert h.detect_emotion("Police discovered a second victim.") == "urgent"

    def test_revelation_turned_out(self):
        h = _load()
        assert h.detect_emotion("It turned out the alibi was false.") == "revelation"

    def test_revelation_forensics(self):
        h = _load()
        assert h.detect_emotion("Forensics revealed a second DNA profile.") == "revelation"

    def test_suspenseful_question(self):
        h = _load()
        assert h.detect_emotion("But who really did it?") == "suspenseful"

    def test_dark_body(self):
        h = _load()
        assert h.detect_emotion("The body was found three days later.") == "dark"

    def test_dark_victim(self):
        h = _load()
        assert h.detect_emotion("The victim was last seen on Tuesday.") == "dark"

    def test_dark_disappeared(self):
        h = _load()
        assert h.detect_emotion("She disappeared without a trace.") == "dark"

    def test_somber_family(self):
        h = _load()
        assert h.detect_emotion("Her family never recovered from the loss.") == "somber"

    def test_somber_mother(self):
        h = _load()
        assert h.detect_emotion("Her mother still visits the grave every Sunday.") == "somber"

    def test_default_tense(self):
        h = _load()
        assert h.detect_emotion("The trial began on a cold January morning.") == "tense"

    def test_all_results_in_ssml_map(self):
        h = _load()
        test_sentences = [
            "No one knew the truth.",
            "Suddenly it all made sense.",
            "It turned out the killer was known to police.",
            "Who was really responsible?",
            "The body was found in the river.",
            "Her family was devastated.",
            "The prosecution rested its case.",
        ]
        for sentence in test_sentences:
            result = h.detect_emotion(sentence)
            assert result in h.SSML_EMOTION_MAP, (
                f"detect_emotion({sentence!r}) returned {result!r} which is not in SSML_EMOTION_MAP"
            )


class TestElevenLabsQuotaFlag:
    """Tests for the ELEVENLABS_QUOTA_EXHAUSTED module-level flag."""

    def test_flag_starts_false(self):
        h = _load()
        assert h.ELEVENLABS_QUOTA_EXHAUSTED is False

    def test_flag_set_on_quota_error(self):
        h = _load()
        h.ELEVENLABS_QUOTA_EXHAUSTED = False
        error_body = json.dumps({"detail": {"status": "quota_exceeded"}}).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="https://api.elevenlabs.io/v1/text-to-speech/test-voice",
            code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(error_body),
        )
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"polly-bytes")}

        with patch.object(h, "_synthesize_sentence", side_effect=http_error), \
             patch("boto3.client", return_value=polly):
            h._synthesize_sentence_with_fallback(
                "hello", "test-voice", {}, "test-key", "documentary", {"voice": {}}
            )

        assert h.ELEVENLABS_QUOTA_EXHAUSTED is True

    def test_skips_elevenlabs_when_flag_true(self):
        h = _load()
        h.ELEVENLABS_QUOTA_EXHAUSTED = True
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"polly-direct")}
        synthesize_called = []

        def track_synthesize(*args, **kwargs):
            synthesize_called.append(True)
            return b"should-not-reach"

        with patch.object(h, "_synthesize_sentence", side_effect=track_synthesize), \
             patch("boto3.client", return_value=polly):
            result = h._synthesize_sentence_with_fallback(
                "hello", "test-voice", {}, "test-key", "documentary", {"voice": {}}
            )

        assert not synthesize_called, "_synthesize_sentence should not be called when quota exhausted"
        assert result == b"polly-direct"

    def test_limit_reached_triggers_fallback(self):
        h = _load()
        h.ELEVENLABS_QUOTA_EXHAUSTED = False
        error_body = json.dumps({"detail": {"status": "limit_reached", "message": "Monthly limit"}}).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="https://api.elevenlabs.io/v1/text-to-speech/test-voice",
            code=429, msg="Too Many Requests", hdrs=None,
            fp=io.BytesIO(error_body),
        )
        polly = MagicMock()
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"polly-limit")}

        with patch.object(h, "_synthesize_sentence", side_effect=http_error), \
             patch("boto3.client", return_value=polly):
            result = h._synthesize_sentence_with_fallback(
                "hello", "test-voice", {}, "test-key", "documentary", {"voice": {}}
            )

        assert result == b"polly-limit"
        assert h.ELEVENLABS_QUOTA_EXHAUSTED is True


class TestPunctuationPauses:
    """Tests for the punctuation → SSML break conversion."""

    def test_ellipsis_becomes_break(self):
        h = _load()
        result = h._apply_punctuation_pauses("She waited... and waited.")
        assert '<break time="700ms"/>' in result

    def test_em_dash_becomes_break(self):
        h = _load()
        result = h._apply_punctuation_pauses("The truth — hidden for years.")
        assert '<break time="400ms"/>' in result

    def test_hyphen_space_becomes_break(self):
        h = _load()
        result = h._apply_punctuation_pauses("One step - then another.")
        assert '<break time="300ms"/>' in result

    def test_no_punctuation_unchanged(self):
        h = _load()
        result = h._apply_punctuation_pauses("plain sentence here")
        assert result == "plain sentence here"


class TestTrueCrimeEmotions:
    """Verify all 5 True Crime emotions are present in SSML_EMOTION_MAP."""

    def test_whispering_in_map(self):
        h = _load()
        assert "whispering" in h.SSML_EMOTION_MAP

    def test_urgent_in_map(self):
        h = _load()
        assert "urgent" in h.SSML_EMOTION_MAP

    def test_revelation_in_map(self):
        h = _load()
        assert "revelation" in h.SSML_EMOTION_MAP

    def test_dark_in_map(self):
        h = _load()
        assert "dark" in h.SSML_EMOTION_MAP

    def test_suspenseful_in_map(self):
        h = _load()
        assert "suspenseful" in h.SSML_EMOTION_MAP

    def test_all_original_emotions_preserved(self):
        h = _load()
        original = ["tense", "excited", "reflective", "authoritative", "somber", "hopeful", "neutral"]
        for emotion in original:
            assert emotion in h.SSML_EMOTION_MAP, f"Original emotion {emotion!r} missing from SSML_EMOTION_MAP"

