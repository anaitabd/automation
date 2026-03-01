#!/usr/bin/env python3
"""
test_check_external.py — Unit tests for check_external.py

All HTTP calls are mocked; no real network traffic.

Run:
    python -m pytest scripts/test_check_external.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from requests.exceptions import ConnectionError, Timeout

from scripts.check_external import (
    check_all,
    check_discord,
    check_pexels,
    MAX_RETRIES,
    PEXELS_URL,
    PEXELS_PARAMS,
    REQUEST_TIMEOUT_S,
)


# ═══════════════════════════════════════════════════════════════════════
# Pexels checks
# ═══════════════════════════════════════════════════════════════════════
class TestCheckPexels:
    """Tests for the Pexels API check."""

    @patch("scripts.check_external.requests.get")
    def test_success_200(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200)

        result = check_pexels("test-api-key")

        assert result == {"ok": True, "status": 200}
        mock_get.assert_called_once_with(
            PEXELS_URL,
            params=PEXELS_PARAMS,
            headers={"Authorization": "test-api-key"},
            timeout=REQUEST_TIMEOUT_S,
        )

    @patch("scripts.check_external.requests.get")
    def test_no_bearer_prefix(self, mock_get: MagicMock) -> None:
        """Authorization header must NOT have 'Bearer' prefix."""
        mock_get.return_value = MagicMock(status_code=200)
        check_pexels("my-key")

        _, kwargs = mock_get.call_args
        auth = kwargs["headers"]["Authorization"]
        assert not auth.startswith("Bearer"), "Must not use Bearer prefix"
        assert auth == "my-key"

    @patch("scripts.check_external.requests.get")
    def test_failure_403(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=403)

        result = check_pexels("bad-key")

        assert result["ok"] is False
        assert result["status"] == 403
        assert "403" in result["error"]

    @patch("scripts.check_external.requests.get")
    def test_failure_500(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=500)

        result = check_pexels("key")

        assert result["ok"] is False
        assert result["status"] == 500

    @patch("scripts.check_external.time.sleep")
    @patch("scripts.check_external.requests.get")
    def test_retry_on_timeout(self, mock_get: MagicMock, mock_sleep: MagicMock) -> None:
        """Timeout on first attempt → retry → succeed on second."""
        mock_get.side_effect = [Timeout("read timed out"), MagicMock(status_code=200)]

        result = check_pexels("key")

        assert result["ok"] is True
        assert result["status"] == 200
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch("scripts.check_external.time.sleep")
    @patch("scripts.check_external.requests.get")
    def test_retry_on_connection_error(self, mock_get: MagicMock, mock_sleep: MagicMock) -> None:
        """ConnectionError on first attempt → retry → succeed."""
        mock_get.side_effect = [ConnectionError("reset"), MagicMock(status_code=200)]

        result = check_pexels("key")

        assert result["ok"] is True
        assert mock_get.call_count == 2

    @patch("scripts.check_external.time.sleep")
    @patch("scripts.check_external.requests.get")
    def test_retry_exhausted(self, mock_get: MagicMock, mock_sleep: MagicMock) -> None:
        """All retries fail → returns error."""
        mock_get.side_effect = Timeout("timed out")

        result = check_pexels("key")

        assert result["ok"] is False
        assert result["status"] is None
        assert "timed out" in result["error"]
        assert mock_get.call_count == MAX_RETRIES

    @patch("scripts.check_external.requests.get")
    def test_no_retry_on_value_error(self, mock_get: MagicMock) -> None:
        """Non-transient errors must NOT be retried."""
        mock_get.side_effect = ValueError("bad url")

        result = check_pexels("key")

        assert result["ok"] is False
        assert mock_get.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# Discord checks
# ═══════════════════════════════════════════════════════════════════════
class TestCheckDiscord:
    """Tests for the Discord Webhook check."""

    WEBHOOK = "https://discord.com/api/webhooks/123/abc"

    @patch("scripts.check_external.requests.post")
    def test_success_204(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=204)

        result = check_discord(self.WEBHOOK)

        assert result == {"ok": True, "status": 204}
        mock_post.assert_called_once_with(
            self.WEBHOOK,
            json={"content": "webhook test"},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
        )

    @patch("scripts.check_external.requests.post")
    def test_no_authorization_header(self, mock_post: MagicMock) -> None:
        """Discord webhook request must NOT carry an Authorization header."""
        mock_post.return_value = MagicMock(status_code=204)
        check_discord(self.WEBHOOK)

        _, kwargs = mock_post.call_args
        headers = kwargs["headers"]
        assert "Authorization" not in headers

    @patch("scripts.check_external.requests.post")
    def test_failure_403(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=403)

        result = check_discord(self.WEBHOOK)

        assert result["ok"] is False
        assert result["status"] == 403
        assert "403" in result["error"]

    @patch("scripts.check_external.requests.post")
    def test_failure_404(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=404)

        result = check_discord(self.WEBHOOK)

        assert result["ok"] is False
        assert result["status"] == 404

    @patch("scripts.check_external.time.sleep")
    @patch("scripts.check_external.requests.post")
    def test_retry_on_timeout(self, mock_post: MagicMock, mock_sleep: MagicMock) -> None:
        mock_post.side_effect = [Timeout("timed out"), MagicMock(status_code=204)]

        result = check_discord(self.WEBHOOK)

        assert result["ok"] is True
        assert mock_post.call_count == 2

    @patch("scripts.check_external.time.sleep")
    @patch("scripts.check_external.requests.post")
    def test_retry_on_connection_reset(self, mock_post: MagicMock, mock_sleep: MagicMock) -> None:
        exc = ConnectionError("Connection reset")
        exc.__cause__ = ConnectionResetError()
        mock_post.side_effect = [exc, MagicMock(status_code=204)]

        result = check_discord(self.WEBHOOK)

        assert result["ok"] is True
        assert mock_post.call_count == 2

    @patch("scripts.check_external.time.sleep")
    @patch("scripts.check_external.requests.post")
    def test_retry_exhausted(self, mock_post: MagicMock, mock_sleep: MagicMock) -> None:
        mock_post.side_effect = ConnectionError("no route to host")

        result = check_discord(self.WEBHOOK)

        assert result["ok"] is False
        assert result["status"] is None
        assert "no route to host" in result["error"]
        assert mock_post.call_count == MAX_RETRIES

    @patch("scripts.check_external.requests.post")
    def test_non_retryable_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = ValueError("bad")

        result = check_discord(self.WEBHOOK)

        assert result["ok"] is False
        assert mock_post.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# check_all (integration-level with mocks)
# ═══════════════════════════════════════════════════════════════════════
class TestCheckAll:
    """Tests for the top-level check_all() orchestrator."""

    @patch("scripts.check_external.requests.post")
    @patch("scripts.check_external.requests.get")
    def test_both_pass(self, mock_get: MagicMock, mock_post: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_post.return_value = MagicMock(status_code=204)

        result = check_all(env={
            "PEXELS_API_KEY": "  my-key  ",
            "DISCORD_WEBHOOK_URL": " https://hook.example.com ",
        })

        assert result["pexels"]["ok"] is True
        assert result["discord"]["ok"] is True
        # Verify whitespace was trimmed
        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "my-key"

    @patch("scripts.check_external.requests.post")
    @patch("scripts.check_external.requests.get")
    def test_both_fail(self, mock_get: MagicMock, mock_post: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=403)
        mock_post.return_value = MagicMock(status_code=403)

        result = check_all(env={
            "PEXELS_API_KEY": "bad",
            "DISCORD_WEBHOOK_URL": "https://bad.hook",
        })

        assert result["pexels"]["ok"] is False
        assert result["discord"]["ok"] is False

    def test_missing_pexels_key(self) -> None:
        result = check_all(env={
            "DISCORD_WEBHOOK_URL": "https://hook.example.com",
        })
        assert result["pexels"]["ok"] is False
        assert "not set" in result["pexels"]["error"]

    def test_missing_discord_url(self) -> None:
        result = check_all(env={
            "PEXELS_API_KEY": "key",
        })
        assert result["discord"]["ok"] is False
        assert "not set" in result["discord"]["error"]

    def test_empty_strings(self) -> None:
        result = check_all(env={
            "PEXELS_API_KEY": "   ",
            "DISCORD_WEBHOOK_URL": "",
        })
        assert result["pexels"]["ok"] is False
        assert result["discord"]["ok"] is False

    @patch("scripts.check_external.requests.post")
    @patch("scripts.check_external.requests.get")
    def test_whitespace_trimmed(self, mock_get: MagicMock, mock_post: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_post.return_value = MagicMock(status_code=204)

        check_all(env={
            "PEXELS_API_KEY": "\n  key123  \n",
            "DISCORD_WEBHOOK_URL": "  https://hook  ",
        })

        # Pexels: trimmed key
        _, get_kw = mock_get.call_args
        assert get_kw["headers"]["Authorization"] == "key123"

        # Discord: trimmed URL
        post_args, _ = mock_post.call_args
        assert post_args[0] == "https://hook"

    @patch("scripts.check_external.requests.post")
    @patch("scripts.check_external.requests.get")
    def test_secrets_not_in_result(self, mock_get: MagicMock, mock_post: MagicMock) -> None:
        """Full secrets must never appear in the returned result dict."""
        secret_key = "super-secret-pexels-key-12345"
        secret_url = "https://discord.com/api/webhooks/999/top-secret-token"

        mock_get.return_value = MagicMock(status_code=403)
        mock_post.return_value = MagicMock(status_code=403)

        result = check_all(env={
            "PEXELS_API_KEY": secret_key,
            "DISCORD_WEBHOOK_URL": secret_url,
        })

        serialised = str(result)
        assert secret_key not in serialised
        assert secret_url not in serialised


