import json
import os
import sys
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")
os.environ.setdefault("ASSETS_BUCKET", "test-assets")
os.environ.setdefault("ECS_SUBNETS", "[]")

_HANDLER_MOD = None


def _load_api_handler():
    global _HANDLER_MOD
    if _HANDLER_MOD is not None:
        return _HANDLER_MOD
    mod_name = "nexus_api_handler_test"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        os.path.join(LAMBDAS_DIR, "nexus-api", "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    with patch("boto3.client"):
        spec.loader.exec_module(mod)
    _HANDLER_MOD = mod
    return mod


class TestHandleRun(unittest.TestCase):
    def setUp(self):
        self.h = _load_api_handler()
        self.sfn_mock = MagicMock()
        self.sfn_mock.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123:execution:test:run-id"
        }
        self.h.sfn = self.sfn_mock

    def test_handle_run_forwards_all_fields(self):
        body = {
            "niche": "technology",
            "profile": "documentary",
            "generate_shorts": True,
            "shorts_tiers": "micro,short",
            "channel_id": "ch123",
        }
        resp = self.h._handle_run(body)
        self.assertEqual(resp["statusCode"], 200)

        call_args = self.sfn_mock.start_execution.call_args
        raw_input = (call_args.kwargs or call_args[1]).get("input", "{}")
        input_data = json.loads(raw_input)
        self.assertTrue(input_data["generate_shorts"])
        self.assertEqual(input_data["shorts_tiers"], "micro,short")
        self.assertEqual(input_data["channel_id"], "ch123")

    def test_handle_run_defaults_optional_fields(self):
        body = {"niche": "finance", "profile": "finance"}
        resp = self.h._handle_run(body)
        self.assertEqual(resp["statusCode"], 200)

        call_args = self.sfn_mock.start_execution.call_args
        raw_input = (call_args.kwargs or call_args[1]).get("input", "{}")
        input_data = json.loads(raw_input)
        self.assertFalse(input_data["generate_shorts"])
        self.assertEqual(input_data["shorts_tiers"], "micro,short,mid,full")
        self.assertIsNone(input_data["channel_id"])

    def test_handle_run_returns_400_if_niche_missing(self):
        resp = self.h._handle_run({"profile": "documentary"})
        self.assertEqual(resp["statusCode"], 400)
        body = json.loads(resp["body"])
        self.assertIn("niche", body["error"].lower())

    def test_handle_run_returns_400_if_niche_too_long(self):
        resp = self.h._handle_run({"niche": "x" * 201, "profile": "documentary"})
        self.assertEqual(resp["statusCode"], 400)

    def test_handle_run_returns_400_if_invalid_profile(self):
        resp = self.h._handle_run({"niche": "tech", "profile": "invalid"})
        self.assertEqual(resp["statusCode"], 400)

    def test_handle_run_returns_400_if_invalid_shorts_tier(self):
        resp = self.h._handle_run({"niche": "tech", "profile": "documentary", "shorts_tiers": "micro,invalid"})
        self.assertEqual(resp["statusCode"], 400)


class TestHandleStatus(unittest.TestCase):
    def _make_sfn_event(self, etype, name, ts_offset=0):
        from datetime import datetime, timezone, timedelta
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ts_offset)
        return {
            "type": etype,
            "timestamp": ts,
            "stateEnteredEventDetails": {"name": name} if "Entered" in etype else {},
            "stateExitedEventDetails": {"name": name} if "Exited" in etype else {},
            "taskFailedEventDetails": {},
        }

    def test_handle_status_returns_correct_step(self):
        h = _load_api_handler()
        sfn_mock = MagicMock()
        from datetime import datetime, timezone
        sfn_mock.describe_execution.return_value = {
            "status": "RUNNING",
            "startDate": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        sfn_mock.get_execution_history.return_value = {
            "events": [
                self._make_sfn_event("TaskStateEntered", "Research", 0),
            ],
        }
        h.sfn = sfn_mock

        resp = h._handle_status("test-run-id")
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        research_step = next(s for s in body["steps"] if s["name"] == "Research")
        self.assertEqual(research_step["status"], "running")


class TestHandleOutputs(unittest.TestCase):
    def test_handle_outputs_returns_presigned_urls(self):
        h = _load_api_handler()
        s3_mock = MagicMock()
        s3_mock.head_object.return_value = {}
        s3_mock.generate_presigned_url.return_value = "https://presigned.url/file.mp4"
        s3_mock.list_objects_v2.return_value = {"Contents": []}
        h.s3 = s3_mock

        resp = h._handle_outputs("test-run-id")
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertGreater(len(body["urls"]), 0)


if __name__ == "__main__":
    unittest.main()
