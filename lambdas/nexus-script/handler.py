"""
nexus-script Lambda
Runtime: Python 3.12 | Memory: 1 GB | Timeout: 10 min

Multi-pass script generation:
  Pass 1 — Structure          (Bedrock claude-opus-4-0)
  Pass 2 — Hook rewrite       (Bedrock claude-opus-4-0)
  Pass 3 — Visual cues        (Bedrock claude-opus-4-0)
  Pass 4 — Pacing polish      (Bedrock claude-opus-4-0)
  Pass 5 — Fact check (finance only, Perplexity sonar-pro)

Writes script JSON to s3://nexus-outputs/{run_id}/script.json.
"""

import json
import time
import boto3
import urllib.request

# ---------------------------------------------------------------------------
# Secrets cache
# ---------------------------------------------------------------------------
_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
S3_OUTPUTS_BUCKET = "nexus-outputs"
BEDROCK_MODEL_ID = "anthropic.claude-opus-4-0"

SCRIPT_JSON_SCHEMA = """{
  "title": "string",
  "description": "string",
  "tags": ["array of strings"],
  "hook": "string",
  "hook_emotion": "tense|excited|curious|dramatic",
  "sections": [{
    "title": "string",
    "content": "string with [PAUSE]/[BEAT]/[BREATH] markers",
    "emotion": "neutral|tense|dramatic|somber|excited|confident",
    "duration_estimate_sec": 0,
    "visual_cue": {
      "search_queries": ["3 specific stock footage terms"],
      "camera_style": "ken_burns_in|ken_burns_out|pan_left|pan_right|static",
      "color_grade": "cinematic_warm|cold_blue|vintage_sepia|high_contrast|clean_corporate|punchy_vibrant",
      "transition_in": "crossfade|cut|zoom_punch|whip|dissolve",
      "overlay_type": "none|lower_third|stat_counter|quote_card",
      "overlay_text": "max 45 chars"
    }
  }],
  "cta": "string",
  "total_duration_estimate": 0,
  "mood": "string"
}"""


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _http_post(url: str, headers: dict, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Bedrock helper
# ---------------------------------------------------------------------------
def _bedrock_call(prompt: str, max_tokens: int = 4096, retries: int = 3) -> str:
    client = boto3.client("bedrock-runtime")
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    for attempt in range(retries):
        try:
            response = client.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            return json.loads(response["body"].read())["content"][0]["text"]
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Pass 1 — Structure (Bedrock)
# ---------------------------------------------------------------------------
def _pass1_structure(topic: str, angle: str, context: str, profile: dict) -> dict:
    target_min = profile.get("script", {}).get("target_duration_min", 10)
    target_max = profile.get("script", {}).get("target_duration_max", 16)
    tone = profile.get("script", {}).get("tone", "authoritative_compelling")
    narrative = profile.get("script", {}).get("narrative_style", "third_person_omniscient")

    prompt = (
        f"You are a professional YouTube scriptwriter. Create a complete script structure for:\n"
        f"Topic: {topic}\nAngle: {angle}\nContext: {context}\n\n"
        f"Requirements:\n"
        f"- Target duration: {target_min}-{target_max} minutes\n"
        f"- Tone: {tone}\n"
        f"- Narrative style: {narrative}\n"
        f"- Include [PAUSE], [BEAT], [BREATH] markers where appropriate\n"
        f"- Return ONLY valid JSON matching this schema (no markdown):\n{SCRIPT_JSON_SCHEMA}"
    )
    raw = _bedrock_call(prompt, max_tokens=8192)
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Pass 2 — Hook rewrite (Bedrock)
# ---------------------------------------------------------------------------
def _pass2_hook_rewrite(script: dict) -> dict:
    prompt = (
        "You are an expert at writing viral YouTube hooks. "
        "Rewrite the given hook to be punchy, emotionally gripping, and impossible to click away from. "
        "Return ONLY a JSON object (no markdown) with keys 'hook' (string) and 'hook_emotion' "
        "(tense|excited|curious|dramatic).\n\n"
        f"Original hook: {script['hook']}\n"
        f"Video topic: {script['title']}\n"
        f"Mood: {script.get('mood', 'neutral')}"
    )
    raw = _bedrock_call(prompt, max_tokens=512)
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        rewrite = json.loads(raw)
        script["hook"] = rewrite.get("hook", script["hook"])
        script["hook_emotion"] = rewrite.get("hook_emotion", script.get("hook_emotion", "curious"))
    except json.JSONDecodeError:
        pass
    return script


# ---------------------------------------------------------------------------
# Pass 3 — Visual cues (Bedrock)
# ---------------------------------------------------------------------------
def _pass3_visual_cues(script: dict, profile: dict) -> dict:
    color_grade = profile.get("visuals", {}).get("color_grade_default", "cinematic_warm")
    transition = profile.get("editing", {}).get("default_transition", "dissolve")

    for i, section in enumerate(script.get("sections", [])):
        prompt = (
            f"Generate precise visual cue metadata for this YouTube script section.\n"
            f"Section title: {section['title']}\n"
            f"Content excerpt: {section['content'][:300]}\n"
            f"Emotion: {section.get('emotion', 'neutral')}\n"
            f"Default color grade: {color_grade}\n"
            f"Default transition: {transition}\n\n"
            "Return ONLY valid JSON (no markdown) with this structure:\n"
            "{\n"
            '  "search_queries": ["term1", "term2", "term3"],\n'
            '  "camera_style": "ken_burns_in|ken_burns_out|pan_left|pan_right|static",\n'
            f'  "color_grade": "{color_grade}",\n'
            '  "transition_in": "crossfade|cut|zoom_punch|whip|dissolve",\n'
            '  "overlay_type": "none|lower_third|stat_counter|quote_card",\n'
            '  "overlay_text": "max 45 chars"\n'
            "}"
        )
        raw = _bedrock_call(prompt, max_tokens=512)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        try:
            cue = json.loads(raw)
        except json.JSONDecodeError:
            cue = {
                "search_queries": [section["title"]],
                "camera_style": "static",
                "color_grade": color_grade,
                "transition_in": transition,
                "overlay_type": "none",
                "overlay_text": "",
            }
        script["sections"][i]["visual_cue"] = cue
    return script


# ---------------------------------------------------------------------------
# Pass 4 — Pacing polish (Bedrock)
# ---------------------------------------------------------------------------
def _pass4_pacing(script: dict, profile: dict) -> dict:
    cpm = profile.get("editing", {}).get("cuts_per_minute_target", 8)
    prompt = (
        f"Polish the pacing of this YouTube script for {cpm} cuts per minute. "
        "Adjust [PAUSE], [BEAT], [BREATH] markers, tighten sentences, and update "
        "duration_estimate_sec for each section. Return the full script JSON "
        "(same schema, no markdown changes, only values updated):\n\n"
        f"{json.dumps(script, indent=2)}"
    )
    raw = _bedrock_call(prompt, max_tokens=8192)
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return script


# ---------------------------------------------------------------------------
# Pass 5 — Fact check (finance, Perplexity)
# ---------------------------------------------------------------------------
def _pass5_fact_check(script: dict, perplexity_key: str) -> dict:
    headers = {
        "Authorization": f"Bearer {perplexity_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "sonar-pro",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a rigorous financial fact-checker. "
                    "Identify any factual claims that need citation or correction, "
                    "and return an updated script with accurate data inserted inline. "
                    "Return ONLY the updated script as valid JSON, same schema."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Fact-check and enrich this finance script:\n{json.dumps(script, indent=2)}"
                ),
            },
        ],
        "max_tokens": 8192,
    }
    result = _http_post(
        "https://api.perplexity.ai/chat/completions", headers=headers, body=body
    )
    raw = result["choices"][0]["message"]["content"].strip()
    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return script


# ---------------------------------------------------------------------------
# Save to S3
# ---------------------------------------------------------------------------
def _save_to_s3(run_id: str, script: dict) -> str:
    s3 = boto3.client("s3")
    key = f"{run_id}/script.json"
    s3.put_object(
        Bucket=S3_OUTPUTS_BUCKET,
        Key=key,
        Body=json.dumps(script, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


# ---------------------------------------------------------------------------
# Error writer
# ---------------------------------------------------------------------------
def _write_error(run_id: str, step: str, exc: Exception) -> None:
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/errors/{step}.json",
            Body=json.dumps({"step": step, "error": str(exc)}).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    topic: str = event["selected_topic"]
    angle: str = event["angle"]
    trending_context: str = event.get("trending_context", "")
    dry_run: bool = event.get("dry_run", False)

    try:
        # Load channel profile from S3
        s3 = boto3.client("s3")
        profile_obj = s3.get_object(Bucket="nexus-config", Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        if dry_run:
            script = {
                "title": f"[DRY RUN] {topic}",
                "description": "Dry run script.",
                "tags": ["dry_run"],
                "hook": "This is a dry run.",
                "hook_emotion": "neutral",
                "sections": [
                    {
                        "title": "Section 1",
                        "content": "Dry run content. [PAUSE]",
                        "emotion": "neutral",
                        "duration_estimate_sec": 60,
                        "visual_cue": {
                            "search_queries": ["test footage"],
                            "camera_style": "static",
                            "color_grade": "cinematic_warm",
                            "transition_in": "dissolve",
                            "overlay_type": "none",
                            "overlay_text": "",
                        },
                    }
                ],
                "cta": "Subscribe for more.",
                "total_duration_estimate": 60,
                "mood": "neutral",
            }
        else:
            perplexity_key = get_secret("nexus/perplexity_api_key")["api_key"]

            # Pass 1: Structure
            script = _pass1_structure(topic, angle, trending_context, profile)
            # Pass 2: Hook rewrite
            script = _pass2_hook_rewrite(script)
            # Pass 3: Visual cues
            script = _pass3_visual_cues(script, profile)
            # Pass 4: Pacing polish
            script = _pass4_pacing(script, profile)
            # Pass 5: Fact check (finance only)
            if profile_name == "finance":
                script = _pass5_fact_check(script, perplexity_key)

        script["run_id"] = run_id
        s3_key = _save_to_s3(run_id, script)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": dry_run,
            "script_s3_key": s3_key,
            "title": script["title"],
            "description": script.get("description", ""),
            "tags": script.get("tags", []),
            "total_duration_estimate": script.get("total_duration_estimate", 0),
            "section_count": len(script.get("sections", [])),
        }

    except Exception as exc:
        _write_error(run_id, "script", exc)
        raise
