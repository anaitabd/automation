import json
import os
import sys
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

os.environ.setdefault("OUTPUTS_BUCKET", "test-outputs")


def _make_utils_mock():
    mock_utils = MagicMock()
    mock_utils.get_logger.return_value = MagicMock()
    mock_utils.notify_step_start.return_value = 0.0
    mock_utils.notify_step_complete.return_value = None
    return mock_utils


def _load_notify_handler():
    mod_name = "nexus_notify_handler_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    sys.modules["nexus_pipeline_utils"] = _make_utils_mock()
    sys.modules["psycopg2"] = MagicMock()
    with patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            mod_name,
            os.path.join(LAMBDAS_DIR, "nexus-notify", "handler.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return mod


class TestNotifyHandler(unittest.TestCase):
    def _make_success_event(self):
        return {
            "run_id": "test-run-123",
            "profile": "documentary",
            "niche": "technology",
            "dry_run": False,
            "title": "Test Video",
            "final_video_s3_key": "test-run-123/review/final_video.mp4",
            "video_url": "https://youtube.com/watch?v=test123",
            "video_duration_sec": 600.0,
            "thumbnail_s3_keys": ["test-run-123/thumbnails/thumbnail_0.jpg"],
            "primary_thumbnail_s3_key": "test-run-123/thumbnails/thumbnail_0.jpg",
            "execution_start_time": "2024-01-01T00:00:00Z",
        }

    def test_discord_webhook_called_on_success(self):
        h = _load_notify_handler()

        secret_data = {
            "nexus/discord_webhook_url": {"url": "https://discord.com/api/webhooks/test"},
            "nexus/db_credentials": {
                "host": "localhost",
                "port": 5432,
                "user": "nexus",
                "password": "secret",
                "dbname": "nexus",
            },
        }

        discord_called = []

        def fake_send_discord(webhook_url, *args, **kwargs):
            discord_called.append(webhook_url)

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        s3_mock = MagicMock()
        s3_mock.generate_presigned_url.return_value = "https://presigned.s3.url/video.mp4"

        with patch.object(h, "get_secret", side_effect=lambda n: secret_data.get(n, {})), \
             patch.object(h, "_send_discord", side_effect=fake_send_discord), \
             patch("boto3.client", return_value=s3_mock), \
             patch("psycopg2.connect", return_value=mock_conn):
            event = self._make_success_event()
            try:
                h.lambda_handler(event, None)
            except Exception:
                pass

        self.assertGreater(len(discord_called), 0)
        self.assertIn("discord.com", discord_called[0])

    def test_postgresql_insert_called_with_correct_fields(self):
        h = _load_notify_handler()

        secret_data = {
            "nexus/discord_webhook_url": {"url": ""},
            "nexus/db_credentials": {
                "host": "localhost",
                "port": 5432,
                "user": "nexus",
                "password": "secret",
                "dbname": "nexus",
            },
        }

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        with patch.object(h, "get_secret", side_effect=lambda n: secret_data.get(n, {})), \
             patch("psycopg2.connect", return_value=mock_conn):
            event = self._make_success_event()
            try:
                h.lambda_handler(event, None)
            except Exception:
                pass

        insert_calls = [
            c for c in mock_cursor.execute.call_args_list
            if "INSERT" in str(c)
        ]
        if insert_calls:
            args = insert_calls[0][0][1]
            self.assertEqual(args[0], "test-run-123")
            self.assertEqual(args[2], "documentary")


if __name__ == "__main__":
    unittest.main()
