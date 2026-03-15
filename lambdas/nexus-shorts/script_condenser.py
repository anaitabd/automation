"""Condense selected script sections into short-form narration via Claude."""

from __future__ import annotations

import json
import boto3


def condense_sections(
    sections: list[dict],
    tier: str,
    target_duration: float,
    profile: dict,
    model_id: str = "",
) -> str:
    """Call Claude to condense script sections into a short-form narration.

    Returns a narration string of 30–160 words depending on tier.
    """
    model = model_id or profile.get("llm", {}).get(
        "condenser_model", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )

    word_targets = {
        "micro": (30, 45),
        "short": (50, 80),
        "mid": (80, 120),
        "full": (110, 160),
    }
    min_words, max_words = word_targets.get(tier, (50, 100))

    # Build section summaries
    section_texts = []
    for i, sec in enumerate(sections):
        content = sec.get("content", sec.get("narration_text", ""))
        title = sec.get("title", f"Section {i + 1}")
        section_texts.append(f"[{title}]\n{content[:500]}")

    combined = "\n\n".join(section_texts)

    prompt = (
        f"You are a viral short-form video scriptwriter. Condense the following script sections "
        f"into a single cohesive narration for a {target_duration:.0f}-second vertical video.\n\n"
        f"REQUIREMENTS:\n"
        f"- Exactly {min_words}–{max_words} words\n"
        f"- Start with an irresistible hook in the first sentence\n"
        f"- Use short, punchy sentences that sound natural when spoken aloud\n"
        f"- End with a mic-drop statement or call to curiosity\n"
        f"- Do NOT include stage directions, [PAUSE], or any markup\n"
        f"- Write ONLY the narration text, nothing else\n\n"
        f"SOURCE SECTIONS:\n{combined}\n\n"
        f"NARRATION ({min_words}–{max_words} words):"
    )

    client = boto3.client("bedrock-runtime")
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    })

    response = client.invoke_model(
        modelId=model,
        body=body,
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(response["body"].read())
    narration = result["content"][0]["text"].strip()

    # Strip any markdown or quotes the LLM might add
    narration = narration.strip('"').strip("'").strip()

    return narration

