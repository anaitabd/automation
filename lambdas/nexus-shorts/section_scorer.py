"""Score script sections 0–100 across 5 dimensions for short-form selection."""

from __future__ import annotations

import json
import re
from typing import Sequence

import boto3

from config import S3_CONFIG_BUCKET

_HOOK_WORDS = {
    "documentary": ["secret", "hidden", "never", "shocking", "ancient", "mystery", "lost", "forbidden"],
    "finance": ["crash", "bubble", "trillion", "bankrupt", "profit", "surge", "collapse", "skyrocket"],
    "entertainment": ["insane", "unbelievable", "epic", "crazy", "viral", "mind-blowing", "legendary"],
}

_EMOTION_WORDS = {
    "documentary": ["terrifying", "devastating", "haunting", "awe-inspiring", "legendary"],
    "finance": ["unprecedented", "catastrophic", "game-changing", "revolutionary", "volatile"],
    "entertainment": ["hilarious", "jaw-dropping", "heartbreaking", "explosive", "shocking"],
}


def score_section(section: dict, profile_name: str) -> int:
    """Score a single section 0–100 across 5 dimensions."""
    score = 0
    content = section.get("content", section.get("narration_text", ""))
    title = section.get("title", "")
    text = f"{title} {content}".lower()
    visual_cue = section.get("visual_cue", {})
    duration = float(section.get("duration_estimate_sec", section.get("estimated_duration", 30)))

    # 1. Hook strength (0–30)
    hook_score = 0
    if "?" in content[:200]:
        hook_score += 10
    hook_words = _HOOK_WORDS.get(profile_name, _HOOK_WORDS["documentary"])
    matches = sum(1 for w in hook_words if w in text)
    hook_score += min(20, matches * 5)
    score += min(30, hook_score)

    # 2. Visual potential (0–25)
    vis_score = 0
    if visual_cue.get("camera_style") and visual_cue["camera_style"] != "static":
        vis_score += 10
    if visual_cue.get("overlay_type") and visual_cue["overlay_type"] != "none":
        vis_score += 8
    if section.get("nova_canvas_prompt") or section.get("nova_reel_prompt"):
        vis_score += 7
    score += min(25, vis_score)

    # 3. Emotional trigger (0–20)
    emo_words = _EMOTION_WORDS.get(profile_name, _EMOTION_WORDS["documentary"])
    emo_matches = sum(1 for w in emo_words if w in text)
    score += min(20, emo_matches * 5)

    # 4. Quotability (0–15)
    sentences = re.split(r"[.!?]+", content)
    short_punchy = sum(1 for s in sentences if 5 <= len(s.split()) <= 15)
    has_numbers = bool(re.search(r"\d{2,}", content))
    quot_score = min(10, short_punchy * 3) + (5 if has_numbers else 0)
    score += min(15, quot_score)

    # 5. Pacing (0–10) — 8–20s sections score highest
    if 8 <= duration <= 20:
        score += 10
    elif 5 <= duration <= 30:
        score += 5
    else:
        score += 2

    return min(100, score)


def select_sections(
    sections: list[dict],
    profile_name: str,
    count: int,
) -> list[dict]:
    """Select the best N sections distributed across the full script.

    Returns sections sorted by their original position in the script.
    Ensures we pick from beginning, middle, and end — never all clustered.
    """
    if len(sections) <= count:
        return list(sections)

    # Score all sections
    scored = [(i, section, score_section(section, profile_name)) for i, section in enumerate(sections)]

    # Divide into thirds
    n = len(sections)
    third = max(1, n // 3)
    groups = [
        scored[:third],
        scored[third:2 * third],
        scored[2 * third:],
    ]

    # Pick proportionally from each group
    selected_indices: list[int] = []
    picks_per_group = max(1, count // 3)
    remainder = count - picks_per_group * 3

    for group in groups:
        group_sorted = sorted(group, key=lambda x: x[2], reverse=True)
        for idx, sec, sc in group_sorted[:picks_per_group]:
            selected_indices.append(idx)

    # Fill remainder from best overall (not yet selected)
    all_sorted = sorted(scored, key=lambda x: x[2], reverse=True)
    for idx, sec, sc in all_sorted:
        if len(selected_indices) >= count:
            break
        if idx not in selected_indices:
            selected_indices.append(idx)

    # Return in original order
    selected_indices.sort()
    return [sections[i] for i in selected_indices]

