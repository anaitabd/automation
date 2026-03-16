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
    def test_bedrock_web_search_calls_invoke_model(self):
        h = _load_research_handler()
        captured = {}

        fake_result = json.dumps({
            "content": [{"type": "text", "text": "trending content about niche"}]
        }).encode("utf-8")

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: fake_result)
        }

        with patch.object(h, "bedrock", mock_bedrock):
            result = h._bedrock_web_search("technology", run_id="test-run")

        self.assertTrue(mock_bedrock.invoke_model.called)
        call_kwargs = mock_bedrock.invoke_model.call_args
        self.assertIsNotNone(call_kwargs)
        raw_body = call_kwargs.kwargs.get("body") or (call_kwargs.args[0] if call_kwargs.args else None)
        body = json.loads(raw_body)
        tools = body.get("tools", [])
        self.assertTrue(any(t.get("type") == "web_search_20250305" for t in tools))
        self.assertIn("trending content", result)

    def test_bedrock_web_search_extracts_text_blocks(self):
        h = _load_research_handler()
        fake_result = json.dumps({
            "content": [
                {"type": "text", "text": "block one"},
                {"type": "tool_use", "name": "web_search", "id": "x"},
                {"type": "text", "text": "block two"},
            ]
        }).encode("utf-8")

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: fake_result)
        }

        with patch.object(h, "bedrock", mock_bedrock):
            result = h._bedrock_web_search("finance", run_id="test-run")

        self.assertIn("block one", result)
        self.assertIn("block two", result)

    def test_bedrock_web_search_propagates_errors(self):
        h = _load_research_handler()
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.side_effect = Exception("Bedrock API error")

        with patch.object(h, "bedrock", mock_bedrock), \
                self.assertRaises(Exception) as ctx:
            h._bedrock_web_search("technology", run_id="test-run")
        self.assertIn("Bedrock", str(type(ctx.exception).__name__) + str(ctx.exception))

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

        with patch.object(h, "bedrock", mock_bedrock):
            result = h._bedrock_select_topic("AI", "trending context", run_id="test-run")

        self.assertIn("selected_topic", result)

    def test_research_uses_sonnet_4_5_model(self):
        h = _load_research_handler()
        fake_response = json.dumps({
            "selected_topic": "AI advancements",
            "angle": "economic impact",
            "trending_context": "GPT-5 launch",
            "search_volume_estimate": "100k/month",
        })

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({
                "content": [{"text": fake_response}]
            }).encode("utf-8"))
        }

        with patch.object(h, "bedrock", mock_bedrock):
            h._bedrock_select_topic("AI", "trending context", run_id="test-run")

        call_kwargs = mock_bedrock.invoke_model.call_args
        self.assertIsNotNone(call_kwargs)
        actual_model_id = call_kwargs.kwargs.get("modelId")
        self.assertEqual(actual_model_id, "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    def test_no_perplexity_secret_fetched(self):
        h = _load_research_handler()
        # Verify that the handler no longer fetches the Perplexity API key
        self.assertFalse(hasattr(h, "_perplexity_search"),
                         "_perplexity_search should be removed from handler")
        self.assertFalse(hasattr(h, "_http_post"),
                         "_http_post should be removed from handler")


if __name__ == "__main__":
    unittest.main()
