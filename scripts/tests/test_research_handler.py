import json
import os
import sys
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("CONFIG_BUCKET", "test-config")


def _make_utils_mock():
    mock_utils = MagicMock()
    mock_utils.get_logger.return_value = MagicMock()
    mock_utils.notify_step_start.return_value = 0.0
    mock_utils.notify_step_complete.return_value = None
    return mock_utils


def _load_research_handler():
    mod_name = "nexus_research_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    with patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            mod_name,
            os.path.join(LAMBDAS_DIR, "nexus-research", "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod


class TestResearchHandler(unittest.TestCase):
    def test_perplexity_call_uses_correct_model(self):
        h = _load_research_handler()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            body = json.loads(req.data.decode("utf-8"))
            captured["model"] = body.get("model", "")
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps({
                "choices": [{"message": {"content": "trending content"}}]
            }).encode("utf-8")
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = h._perplexity_search("technology", "test-api-key")

        self.assertEqual(captured.get("model"), "sonar-pro")
        self.assertIsInstance(result, str)
        self.assertIn("trending content", result)

    def test_fallback_if_perplexity_fails(self):
        h = _load_research_handler()

        with patch("urllib.request.urlopen", side_effect=Exception("Perplexity API error")):
            try:
                result = h._perplexity_search("technology", "test-api-key")
                self.fail("Expected exception to propagate")
            except Exception as exc:
                self.assertIn("Perplexity", str(type(exc).__name__) + str(exc))

    def test_bedrock_select_topic_returns_topic(self):
        h = _load_research_handler()
        fake_response = json.dumps({
            "selected_topic": "AI advancements",
            "angle": "economic impact",
            "trending_context": "GPT-5 launch",
        })

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({
                "content": [{"text": fake_response}]
            }).encode("utf-8"))
        }

        with patch("boto3.client", return_value=mock_bedrock):
            result = h._bedrock_select_topic("AI", "trending context", "anthropic.claude-3-sonnet-20240229-v1:0")

        self.assertIn("selected_topic", result)


if __name__ == "__main__":
    unittest.main()
