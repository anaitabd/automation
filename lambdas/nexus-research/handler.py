import json
import os
import random
import threading
import time
import uuid
import boto3
from botocore.exceptions import ClientError
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-research")

_cache: dict = {}

bedrock = boto3.client("bedrock-runtime")
bedrock_semaphore = threading.Semaphore(4)


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


def invoke_with_backoff(fn, payload: dict, run_id: str = "", max_retries: int = 5) -> dict:
    """Invoke a Bedrock API callable with semaphore + exponential backoff on ThrottlingException."""
    for attempt in range(max_retries):
        try:
            with bedrock_semaphore:
                return fn(**payload)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ThrottlingException" and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                log.warning(
                    f"[{run_id}] bedrock: throttled attempt "
                    f"{attempt+1}/{max_retries}, retrying in {wait:.1f}s"
                )
                time.sleep(wait)
            else:
                raise


S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")
BEDROCK_MODEL_ID_DEFAULT = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def _bedrock_web_search(query: str, run_id: str, model_id: str = "") -> str:
    """Research YouTube trends using Bedrock Claude with native web search tool."""
    _model = model_id or BEDROCK_MODEL_ID_DEFAULT
    prompt = (
        f"Research the niche '{query}' on YouTube. "
        "List the top 5 trending subtopics, typical view counts, best-performing "
        "video angles, and audience pain points. Be specific and data-driven."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "system": (
            "You are a YouTube trend analyst. Provide concise, data-backed"
            " insights about trending topics, search volume, and engagement angles."
        ),
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    })
    payload = {
        "modelId": _model,
        "body": body,
        "contentType": "application/json",
        "accept": "application/json",
    }
    log.info(f"[{run_id}] research: calling Bedrock web search for niche: {query}")
    response = invoke_with_backoff(bedrock.invoke_model, payload, run_id=run_id)
    result = json.loads(response["body"].read())
    text_parts = [
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(text_parts)


def _bedrock_select_topic(niche: str, research_context: str, run_id: str = "", model_id: str = "") -> dict:
    bedrock_model = model_id or BEDROCK_MODEL_ID_DEFAULT
    prompt = (
        f"You are an expert YouTube strategist. Based on the following research about '{niche}', "
        "select the single best video topic and angle to maximise views and watch time.\n\n"
        f"Research:\n{research_context}\n\n"
        "Respond ONLY with a JSON object (no markdown) with these exact keys:\n"
        "  selected_topic (string)\n"
        "  angle (string)\n"
        "  trending_context (string, 2-3 sentences)\n"
        "  search_volume_estimate (string, e.g. '120k/month')"
    )
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    payload = {
        "modelId": bedrock_model,
        "body": body,
        "contentType": "application/json",
        "accept": "application/json",
    }
    response = invoke_with_backoff(bedrock.invoke_model, payload, run_id=run_id)
    raw = json.loads(response["body"].read())["content"][0]["text"]
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


def _save_to_s3(run_id: str, data: dict) -> str:
    s3 = boto3.client("s3")
    key = f"{run_id}/research.json"
    s3.put_object(
        Bucket=S3_OUTPUTS_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def lambda_handler(event: dict, context) -> dict:
    run_id = event.get("run_id") or str(uuid.uuid4())
    niche: str = event["niche"]
    profile: str = event.get("profile", "documentary")
    dry_run: bool = event.get("dry_run", False)

    step_start = notify_step_start("research", run_id, niche=niche, profile=profile, dry_run=dry_run)

    try:
        # Load profile to get the configured LLM model
        s3 = boto3.client("s3")
        try:
            profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile}.json")
            profile_data: dict = json.loads(profile_obj["Body"].read())
        except Exception:
            profile_data = {}
        bedrock_model = profile_data.get("llm", {}).get("script_model", BEDROCK_MODEL_ID_DEFAULT)

        if dry_run:
            log.info(f"[{run_id}] research: DRY RUN mode — skipping real API calls")
            research_result = {
                "selected_topic": f"[DRY RUN] Top story in {niche}",
                "angle": "Untold history angle",
                "trending_context": "Dry run — no real API calls made.",
                "search_volume_estimate": "N/A",
            }
        else:
            log.info(f"[{run_id}] research: performing Bedrock web search for niche: {niche}")
            trending_context = _bedrock_web_search(niche, run_id=run_id, model_id=bedrock_model)
            log.info(f"[{run_id}] research: web search returned {len(trending_context)} chars — selecting topic via Bedrock")
            research_result = _bedrock_select_topic(niche, trending_context, run_id=run_id, model_id=bedrock_model)
            log.info(f"[{run_id}] research: selected topic: {research_result.get('selected_topic', '?')[:80]}")

        research_result["run_id"] = run_id
        research_result["niche"] = niche
        research_result["profile"] = profile

        log.info("Saving research to S3")
        s3_key = _save_to_s3(run_id, research_result)

        elapsed = time.time() - step_start
        notify_step_complete("research", run_id, [
            {"name": "Topic", "value": research_result["selected_topic"][:100], "inline": False},
            {"name": "Angle", "value": research_result["angle"][:100], "inline": False},
            {"name": "Search Volume", "value": research_result.get("search_volume_estimate", "N/A"), "inline": True},
            {"name": "Profile", "value": profile, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0x3498DB)

        return {
            "run_id": run_id,
            "profile": profile,
            "dry_run": dry_run,
            "research_s3_key": s3_key,
            "selected_topic": research_result["selected_topic"],
            "angle": research_result["angle"],
            "trending_context": research_result.get("trending_context", ""),
            "search_volume_estimate": research_result.get("search_volume_estimate", ""),
        }

    except Exception as exc:
        log.error("Research step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "research", exc)
        raise


def _write_error(run_id: str, step: str, exc: Exception) -> None:
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/errors/{step}.json",
            Body=json.dumps({"step": step, "error": str(exc)}, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass


