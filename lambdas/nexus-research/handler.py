import json
import time
import uuid
import boto3
import urllib.request
import urllib.error

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


S3_OUTPUTS_BUCKET = "nexus-outputs"
BEDROCK_MODEL_ID = "anthropic.claude-opus-4-0"


def _http_post(url: str, headers: dict, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _perplexity_search(query: str, api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "sonar-pro",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a YouTube trend analyst. Provide concise, data-backed"
                    " insights about trending topics, search volume, and engagement angles."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Research the niche '{query}' on YouTube. "
                    "List the top 5 trending subtopics, typical view counts, best-performing "
                    "video angles, and audience pain points. Be specific and data-driven."
                ),
            },
        ],
        "max_tokens": 2048,
    }
    result = _http_post(
        "https://api.perplexity.ai/chat/completions", headers=headers, body=body
    )
    return result["choices"][0]["message"]["content"]


def _bedrock_select_topic(niche: str, perplexity_context: str) -> dict:
    client = boto3.client("bedrock-runtime")
    prompt = (
        f"You are an expert YouTube strategist. Based on the following research about '{niche}', "
        "select the single best video topic and angle to maximise views and watch time.\n\n"
        f"Research:\n{perplexity_context}\n\n"
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
    retries = 3
    for attempt in range(retries):
        try:
            response = client.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            raw = json.loads(response["body"].read())["content"][0]["text"]
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(raw)
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


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

    try:
        perplexity_key = get_secret("nexus/perplexity_api_key")["api_key"]

        if dry_run:
            research_result = {
                "selected_topic": f"[DRY RUN] Top story in {niche}",
                "angle": "Untold history angle",
                "trending_context": "Dry run — no real API calls made.",
                "search_volume_estimate": "N/A",
            }
        else:
            trending_context = _perplexity_search(niche, perplexity_key)
            research_result = _bedrock_select_topic(niche, trending_context)

        research_result["run_id"] = run_id
        research_result["niche"] = niche
        research_result["profile"] = profile

        s3_key = _save_to_s3(run_id, research_result)

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
