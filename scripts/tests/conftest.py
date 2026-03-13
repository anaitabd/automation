"""Shared test fixtures for Nexus Cloud Lambda handlers.

Uses importlib to load handlers without real AWS connectivity by patching boto3.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LAMBDAS_DIR = os.path.join(REPO_ROOT, "lambdas")

# Default env vars needed by handlers at import time
_DEFAULT_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:nexus-pipeline",
    "OUTPUTS_BUCKET": "test-outputs",
    "ASSETS_BUCKET": "test-assets",
    "CONFIG_BUCKET": "test-config",
    "ECS_SUBNETS": "[]",
    "REQUIRE_API_KEY": "false",
    "CHANNEL_SETUP_FUNCTION": "nexus-channel-setup",
    "BRAND_DESIGNER_FUNCTION": "nexus-brand-designer",
    "LOGO_GEN_FUNCTION": "nexus-logo-gen",
    "INTRO_OUTRO_FUNCTION": "nexus-intro-outro",
    "BRAND_MODEL_ID": "anthropic.claude-sonnet-4-20250514-v1:0",
    "SHORTS_ENABLED": "true",
    "SHORTS_TIERS": "micro,short,mid,full",
}

# Module cache to avoid re-loading
_module_cache: dict = {}


def _ensure_env():
    for k, v in _DEFAULT_ENV.items():
        os.environ.setdefault(k, v)


def load_handler(lambda_name: str, handler_path: str | None = None):
    """Load a Lambda handler module with boto3 patched out.

    Args:
        lambda_name: e.g. 'nexus-api', 'nexus-research'
        handler_path: override path to handler.py; defaults to lambdas/<lambda_name>/handler.py
    """
    cache_key = lambda_name
    if cache_key in _module_cache:
        return _module_cache[cache_key]

    _ensure_env()

    if handler_path is None:
        handler_path = os.path.join(LAMBDAS_DIR, lambda_name, "handler.py")

    mod_name = f"test_{lambda_name.replace('-', '_')}_mod"
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod

    # Add the lambda's own directory to sys.path so local imports work
    lambda_dir = os.path.dirname(handler_path)
    if lambda_dir not in sys.path:
        sys.path.insert(0, lambda_dir)

    with patch("boto3.client", return_value=MagicMock()):
        try:
            spec.loader.exec_module(mod)
        except Exception:
            # Some modules may fail on import due to missing deps — still cache
            pass

    _module_cache[cache_key] = mod
    return mod


@pytest.fixture
def mock_s3():
    """Return a MagicMock configured as an S3 client."""
    client = MagicMock()
    client.head_object.return_value = {}
    client.head_bucket.return_value = {}
    client.generate_presigned_url.return_value = "https://presigned.url/file.mp4"
    client.list_objects_v2.return_value = {"Contents": []}
    client.get_object.return_value = {
        "Body": MagicMock(read=lambda: b'{}')
    }
    client.put_object.return_value = {}
    return client


@pytest.fixture
def mock_sfn():
    """Return a MagicMock configured as a Step Functions client."""
    from datetime import datetime, timezone
    client = MagicMock()
    client.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:123:execution:test:run-id"
    }
    client.describe_execution.return_value = {
        "status": "RUNNING",
        "startDate": datetime(2024, 1, 1, tzinfo=timezone.utc),
    }
    client.get_execution_history.return_value = {"events": []}
    # Attach exceptions
    client.exceptions = MagicMock()
    client.exceptions.ExecutionDoesNotExist = type("ExecutionDoesNotExist", (Exception,), {})
    return client


@pytest.fixture
def mock_lambda_client():
    """Return a MagicMock configured as a Lambda invoke client."""
    client = MagicMock()
    client.invoke.return_value = {
        "Payload": MagicMock(read=lambda: b'{"statusCode": 200}'),
        "StatusCode": 200,
    }
    return client

