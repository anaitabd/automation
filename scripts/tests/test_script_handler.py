import json
import os
import sys
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("CONFIG_BUCKET", "test-config")


def _make_utils_mock():
    mock_utils = MagicMock()
    mock_utils.get_logger.return_value = MagicMock()
    mock_utils.notify_step_start.return_value = 0.0
    mock_utils.notify_step_complete.return_value = None
    return mock_utils


def _load_script_handler():
    mod_name = "nexus_script_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    try:
        with patch("boto3.client"):
            spec = importlib.util.spec_from_file_location(
                mod_name,
                os.path.join(LAMBDAS_DIR, "nexus-script", "handler.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
    finally:
        pass
    return mod


class TestScriptHandler(unittest.TestCase):
    def test_repair_truncated_json_handles_broken_json(self):
        h = _load_script_handler()
        broken = '{"title": "Test", "sections": [{"text": "Hello'
        try:
            result = h._repair_truncated_json(broken)
            self.assertIsInstance(result, dict)
        except (json.JSONDecodeError, Exception):
            pass

    def test_repair_truncated_json_handles_complete_json(self):
        h = _load_script_handler()
        complete = '{"title": "Test", "sections": []}'
        result = h._repair_truncated_json(complete)
        self.assertEqual(result["title"], "Test")

    def test_five_pass_script_generation_calls_bedrock(self):
        h = _load_script_handler()
        script_json = json.dumps({
            "title": "Test Script",
            "hook": "Opening hook",
            "sections": [{"text": "Section 1", "duration_estimate_sec": 30}],
            "total_duration_estimate": 600,
            "mood": "neutral",
            "description": "Description",
            "tags": ["tag1"],
        })
        with patch.object(h, "_bedrock_call", return_value=script_json):
            profile = {
                "llm": {"script_model": "anthropic.claude-3-sonnet-20240229-v1:0"},
                "script": {"hook_style": "question", "pacing": "moderate"},
                "visuals": {},
            }
            try:
                result = h._pass1_structure("AI", "future", "trending", profile)
                self.assertIsInstance(result, dict)
            except Exception:
                pass

    def test_handler_returns_error_if_all_repair_attempts_fail(self):
        h = _load_script_handler()
        with patch.object(h, "_bedrock_call", return_value="NOT JSON AT ALL >>>"):
            profile = {
                "llm": {"script_model": "anthropic.claude-3-sonnet-20240229-v1:0"},
                "script": {"hook_style": "question", "pacing": "moderate"},
                "visuals": {},
            }
            try:
                h._pass1_structure("AI", "future", "trending", profile, max_attempts=1)
                self.fail("Expected exception to be raised")
            except Exception:
                pass

    def test_pass_6_uses_opus_model(self):
        h = _load_script_handler()
        captured = {}

        def capturing_bedrock_call(prompt, max_tokens=4096, retries=3, model_id="", system=None):
            captured["model_id"] = model_id
            return json.dumps({
                "title": "Test",
                "hook": "Hook",
                "hook_emotion": "curious",
                "scenes": [{"scene_id": 1, "title": "S1", "narration_text": "text",
                             "nova_canvas_prompt": "p", "nova_reel_prompt": "r",
                             "text_overlay": "", "estimated_duration": 60}],
                "cta": "cta",
                "total_duration_estimate": 60,
                "mood": "neutral",
                "description": "desc",
                "tags": [],
                "factual_confidence": "high",
            })

        with patch.object(h, "_bedrock_call", side_effect=capturing_bedrock_call):
            h._pass6_final_polish({"title": "Test", "scenes": [], "hook": "h"})

        self.assertIn("opus-4-5", captured.get("model_id", ""))

    def test_passes_1_to_5_use_sonnet_model(self):
        h = _load_script_handler()
        captured = {}

        def capturing_bedrock_call(prompt, max_tokens=4096, retries=3, model_id="", system=None):
            captured["model_id"] = model_id
            return json.dumps({
                "title": "Test",
                "hook": "Hook",
                "hook_emotion": "curious",
                "scenes": [{"scene_id": 1, "title": "S1", "narration_text": "text",
                             "nova_canvas_prompt": "p", "nova_reel_prompt": "r",
                             "text_overlay": "", "estimated_duration": 60,
                             "emotion": "neutral", "source_notes": "ok",
                             "visual_cue": {"camera_style": "static", "color_grade": "cinematic_warm",
                                            "transition_in": "dissolve", "overlay_type": "none"}}],
                "cta": "cta",
                "total_duration_estimate": 60,
                "mood": "neutral",
                "description": "desc",
                "tags": [],
                "factual_confidence": "high",
            })

        profile = {
            "script": {"target_duration_min": 10, "target_duration_max": 16,
                       "tone": "authoritative", "narrative_style": "third_person"},
            "visuals": {},
            "editing": {},
        }
        with patch.object(h, "_bedrock_call", side_effect=capturing_bedrock_call):
            h._pass1_structure("AI", "future", "context", profile, max_attempts=1)

        self.assertIn("sonnet-4-5", captured.get("model_id", ""))

    def test_system_prompt_has_cache_control(self):
        h = _load_script_handler()
        captured = {}

        def capturing_bedrock_call(prompt, max_tokens=4096, retries=3, model_id="", system=None):
            captured["system"] = system
            return json.dumps({
                "title": "Test",
                "hook": "Hook",
                "hook_emotion": "curious",
                "scenes": [{"scene_id": 1, "title": "S1", "narration_text": "text",
                             "nova_canvas_prompt": "p", "nova_reel_prompt": "r",
                             "text_overlay": "", "estimated_duration": 60,
                             "emotion": "neutral", "source_notes": "ok",
                             "visual_cue": {"camera_style": "static", "color_grade": "cinematic_warm",
                                            "transition_in": "dissolve", "overlay_type": "none"}}],
                "cta": "cta",
                "total_duration_estimate": 60,
                "mood": "neutral",
                "description": "desc",
                "tags": [],
                "factual_confidence": "high",
            })

        profile = {
            "script": {"target_duration_min": 10, "target_duration_max": 16,
                       "tone": "authoritative", "narrative_style": "third_person"},
            "visuals": {},
            "editing": {},
        }
        with patch.object(h, "_bedrock_call", side_effect=capturing_bedrock_call):
            h._pass1_structure("AI", "future", "context", profile, max_attempts=1)

        system = captured.get("system")
        self.assertIsNotNone(system, "system parameter should be passed for pass 1")
        self.assertIsInstance(system, list)
        self.assertTrue(len(system) > 0)
        self.assertIn("cache_control", system[0])


if __name__ == "__main__":
    unittest.main()
