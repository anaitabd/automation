"""Condense selected script sections into short-form narration via Claude."""

from __future__ import annotations

import json
import boto3


# True Crime word targets differ from generic defaults
_TRUE_CRIME_WORD_TARGETS: dict[str, tuple[int, int]] = {
    "micro": (35, 45),   # 15s tier: 35-45 words
    "short": (80, 100),  # 30s tier: 80-100 words
    "mid": (120, 150),
    "full": (150, 190),
}

_GENERIC_WORD_TARGETS: dict[str, tuple[int, int]] = {
    "micro": (30, 45),
    "short": (50, 80),
    "mid": (80, 120),
    "full": (110, 160),
}


def _build_true_crime_prompt(
    combined: str,
    tier: str,
    target_duration: float,
    min_words: int,
    max_words: int,
) -> str:
    """Build a True Crime-specific condensation prompt."""
    return (
        f"You are a True Crime short-form video scriptwriter. Condense the following script "
        f"into a gripping {target_duration:.0f}-second vertical video narration.\n\n"
        f"STRICT TRUE CRIME FORMAT:\n"
        f"1. OPEN with the single most shocking sentence from the story — no buildup, no intro\n"
        f"2. 3-sentence maximum setup covering: who, what, where\n"
        f"3. END with an unanswered question that drives viewers to watch the full video\n"
        f"   — never reveal the ending, always leave it open\n\n"
        f"REQUIREMENTS:\n"
        f"- Exactly {min_words}–{max_words} words\n"
        f"- Short, punchy sentences that build tension\n"
        f"- Never summarize the resolution or outcome\n"
        f"- Do NOT include stage directions, [PAUSE], or any markup\n"
        f"- Write ONLY the narration text, nothing else\n\n"
        f"SOURCE SCRIPT:\n{combined}\n\n"
        f"TRUE CRIME NARRATION ({min_words}–{max_words} words):"
    )


def _build_generic_prompt(
    combined: str,
    tier: str,
    target_duration: float,
    min_words: int,
    max_words: int,
) -> str:
    """Build a generic short-form condensation prompt."""
    return (
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


def condense_sections(
    sections: list[dict],
    tier: str,
    target_duration: float,
    profile: dict,
    model_id: str = "",
) -> str:
    """Call Claude to condense script sections into a short-form narration.

    For True Crime profiles, applies a shock-open / 3-sentence setup / open-ending format.
    Returns a narration string of 30–160 words depending on tier.
    """
    model = model_id or profile.get("llm", {}).get(
        "condenser_model", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )

    is_true_crime = profile.get("script", {}).get("style", "") == "true_crime"

    word_targets = _TRUE_CRIME_WORD_TARGETS if is_true_crime else _GENERIC_WORD_TARGETS
    min_words, max_words = word_targets.get(tier, (50, 100))

    # Build section summaries
    section_texts = []
    for i, sec in enumerate(sections):
        content = sec.get("content", sec.get("narration_text", ""))
        title = sec.get("title", f"Section {i + 1}")
        section_texts.append(f"[{title}]\n{content[:500]}")

    combined = "\n\n".join(section_texts)

    if is_true_crime:
        prompt = _build_true_crime_prompt(combined, tier, target_duration, min_words, max_words)
    else:
        prompt = _build_generic_prompt(combined, tier, target_duration, min_words, max_words)

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

