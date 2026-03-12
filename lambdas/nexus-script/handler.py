import json
import os
import re
import time
import boto3
import urllib.request
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-script")

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

_cache: dict = {}


def _repair_truncated_json(text: str) -> dict:
    """Attempt to repair JSON that was truncated mid-stream by the LLM.

    Handles truncation at any point: mid-string, mid-key, mid-number,
    after a colon, after a comma, etc.  Closes unclosed strings, strips
    dangling structural tokens, then closes brackets/braces in stack order.
    """
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No opening brace found", text, 0)

    fragment = text[start:]

    # ── Phase 1: close an unclosed string literal ──
    in_string = False
    escape = False
    for ch in fragment:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        # Escape any trailing backslash that would eat our closing quote
        if fragment.endswith("\\"):
            fragment += '\\"'
        else:
            fragment += '"'

    # ── Phase 2: iteratively strip dangling structural tokens ──
    # After closing the string we may have fragments like:
    #   ..."value"           (ok — value is complete)
    #   ..."key":            (dangling key with no value)
    #   ..."key": "val",     (trailing comma)
    #   ..."key": 123        (ok, but number may be truncated — keep it)
    #   ..."key": tru        (truncated literal)
    # Loop until stable.
    _dangling_patterns = [
        # trailing comma (possibly with whitespace)
        re.compile(r",\s*$"),
        # key with colon but no value:  , "key" :   or  "key" :
        re.compile(r',?\s*"(?:[^"\\]|\\.)*"\s*:\s*$'),
        # key + colon + truncated literal (tru, fals, nul, etc.)
        re.compile(r',?\s*"(?:[^"\\]|\\.)*"\s*:\s*(?:t(?:r(?:ue?)?)?|f(?:a(?:l(?:se?)?)?)?|n(?:u(?:ll?)?)?)$'),
        # key + colon + truncated number (trailing dot with no decimals)
        re.compile(r',?\s*"(?:[^"\\]|\\.)*"\s*:\s*-?\d+\.\s*$'),
        # orphaned bare string after comma:  , "key"  (no colon — left behind after prior strip)
        re.compile(r',\s*"(?:[^"\\]|\\.)*"\s*$'),
    ]
    for _ in range(6):
        stripped = fragment.rstrip()
        changed = False
        for pat in _dangling_patterns:
            m = pat.search(stripped)
            if m and m.end() == len(stripped):
                stripped = stripped[: m.start()]
                changed = True
        if not changed:
            break
        fragment = stripped

    fragment = fragment.rstrip()

    # ── Phase 3: count unclosed braces/brackets and close them ──
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in fragment:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in ("{", "["):
            stack.append(ch)
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()

    # Final trailing-comma strip before closing
    fragment = re.sub(r",\s*$", "", fragment.rstrip())

    for opener in reversed(stack):
        fragment += "]" if opener == "[" else "}"

    # ── Phase 4: try to parse; if it fails, do progressively aggressive scrubs ──
    try:
        return json.loads(fragment)
    except json.JSONDecodeError:
        pass

    # Strategy A: remove the last incomplete object from any array.
    # This handles the common case where truncation hit mid-object inside
    # the "sections" array:  [..., {complete}, {partial  →  [..., {complete}]
    scrub_a = fragment
    for _ in range(3):
        # Find the last  , { ... (no matching close) before a ] or end
        m = re.search(
            r',\s*\{[^{}]*$',
            scrub_a,
        )
        if m:
            scrub_a = scrub_a[: m.start()]
            # Re-close brackets
            _stack: list[str] = []
            _in = False
            _esc = False
            for c in scrub_a:
                if _esc:
                    _esc = False
                    continue
                if c == "\\":
                    _esc = True
                    continue
                if c == '"':
                    _in = not _in
                    continue
                if _in:
                    continue
                if c in ("{", "["):
                    _stack.append(c)
                elif c == "}" and _stack and _stack[-1] == "{":
                    _stack.pop()
                elif c == "]" and _stack and _stack[-1] == "[":
                    _stack.pop()
            scrub_a = re.sub(r",\s*$", "", scrub_a.rstrip())
            for opener in reversed(_stack):
                scrub_a += "]" if opener == "[" else "}"
            try:
                return json.loads(scrub_a)
            except json.JSONDecodeError:
                continue
        else:
            break

    # Strategy B: remove the last key-value pair entirely (original approach,
    # but now using DOTALL and greedy inner match for nested values).
    scrubbed = re.sub(
        r',\s*"(?:[^"\\]|\\.)*"\s*:\s*(?:"(?:[^"\\]|\\.)*"|[\d.eE+\-]+|\[[^]]*]|\{[^}]*}|true|false|null)\s*(?=[}\]])',
        "",
        fragment,
        count=1,
        flags=re.DOTALL,
    )
    if scrubbed != fragment:
        try:
            return json.loads(scrubbed)
        except json.JSONDecodeError:
            pass

    # Last resort — raise so the caller knows repair failed
    return json.loads(fragment)  # will raise JSONDecodeError with details


def _extract_json(raw: str) -> dict:
    """Robustly extract a JSON object from an LLM response.

    Handles markdown fences, preamble text, trailing garbage, and
    truncated output (unclosed strings/braces from token limit).
    Raises json.JSONDecodeError only after all strategies are exhausted.
    """
    if not raw or not raw.strip():
        raise json.JSONDecodeError("Empty response from LLM", raw or "", 0)

    text = raw.strip()

    # 1) Strip markdown fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # 2) Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3) Find the first { … } balanced block
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    # 4) Attempt to repair truncated JSON (LLM hit token limit)
    try:
        result = _repair_truncated_json(text)
        print("[INFO] _extract_json: repaired truncated JSON successfully")
        return result
    except (json.JSONDecodeError, Exception) as repair_exc:
        print(f"[WARN] _extract_json: repair also failed: {repair_exc}")
        pass

    # 5) Fallback: use json_repair library (handles many more edge cases)
    if repair_json is not None:
        try:
            repaired = repair_json(text, return_objects=True)
            if isinstance(repaired, dict):
                print("[INFO] _extract_json: json_repair library recovered JSON successfully")
                return repaired
            # If it returned a list or string, try to find a dict inside
            if isinstance(repaired, list):
                for item in repaired:
                    if isinstance(item, dict):
                        print("[INFO] _extract_json: json_repair library recovered JSON (from list)")
                        return item
            print(f"[WARN] _extract_json: json_repair returned non-dict type: {type(repaired).__name__}")
        except Exception as lib_exc:
            print(f"[WARN] _extract_json: json_repair library also failed: {lib_exc}")

    # 6) Nothing worked — raise with a helpful snippet
    raise json.JSONDecodeError(
        f"Could not extract JSON from LLM response (first 200 chars): {text[:200]}",
        text,
        0,
    )


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")
BEDROCK_MODEL_ID_DEFAULT = "anthropic.claude-3-5-sonnet-20241022-v2:0"

# Set dynamically per-invocation from the profile's llm.script_model
_active_model_id: str = BEDROCK_MODEL_ID_DEFAULT

SCRIPT_JSON_SCHEMA = """{
  "title": "string",
  "description": "string",
  "tags": ["array of strings"],
  "hook": "string",
  "hook_emotion": "tense|excited|curious|dramatic",
  "scenes": [{
    "scene_id": "integer starting at 1",
    "title": "string",
    "narration_text": "string — MINIMUM 150 words of narration per scene. Use [PAUSE]/[BEAT]/[BREATH] markers for pacing. Use [NEEDS SOURCE] for unverifiable claims and [UNVERIFIED: claim] for uncertain facts. 6-10 substantial sentences per scene (never bullet points).",
    "nova_canvas_prompt": "string — detailed text-to-image prompt for Amazon Nova Canvas describing the base image: subject, setting, lighting, style. Example: 'Cinematic aerial photograph of ancient Roman ruins at golden hour, dramatic shadows, photorealistic, high detail'",
    "nova_reel_prompt": "string — camera movement and motion description for Amazon Nova Reel. Example: 'Slow cinematic push-in toward the ruins, subtle parallax depth, golden-hour lighting shifts, 3D ease in/out'",
    "text_overlay": "string — short typewriter-effect text to animate on screen, max 80 chars. Leave empty string if none.",
    "estimated_duration": 90,
    "emotion": "neutral|tense|dramatic|somber|excited|confident",
    "source_notes": "brief note on factual basis for this scene (e.g. 'well-documented historical event' or 'common knowledge' or 'requires verification')",
    "visual_cue": {
      "camera_style": "ken_burns_in|ken_burns_out|pan_left|pan_right|static|slow_drift|dolly_in",
      "color_grade": "cinematic_warm|cold_blue|vintage_sepia|high_contrast|clean_corporate|punchy_vibrant",
      "transition_in": "crossfade|cut|zoom_punch|whip|dissolve|fade_black|wipeleft",
      "overlay_type": "none|lower_third|stat_counter|quote_card"
    }
  }],
  "cta": "string",
  "total_duration_estimate": 900,
  "mood": "string",
  "factual_confidence": "high|medium|low — overall assessment of factual reliability"
}"""

EDL_REQUIRED_SCENE_FIELDS = [
    "scene_id",
    "narration_text",
    "nova_canvas_prompt",
    "nova_reel_prompt",
    "text_overlay",
    "estimated_duration",
]


def _validate_edl_schema(script: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(script.get("scenes"), list) or not script["scenes"]:
        errors.append("'scenes' must be a non-empty list")
        return errors
    for i, scene in enumerate(script["scenes"]):
        if not isinstance(scene, dict):
            errors.append(f"scenes[{i}] is not a dict")
            continue
        for field in EDL_REQUIRED_SCENE_FIELDS:
            if field not in scene:
                errors.append(f"scenes[{i}] missing required field '{field}'")
        if not isinstance(scene.get("estimated_duration"), (int, float)) or scene.get("estimated_duration", 0) <= 0:
            errors.append(f"scenes[{i}].estimated_duration must be a positive number")
        if not isinstance(scene.get("scene_id"), int):
            errors.append(f"scenes[{i}].scene_id must be an integer")
    return errors


def _http_post(url: str, headers: dict, body: dict, retries: int = 3) -> dict:
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries):
        try:
            merged = {"User-Agent": "NexusCloud/1.0"}
            merged.update(headers)
            req = urllib.request.Request(url, data=data, headers=merged, method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _bedrock_call(prompt: str, max_tokens: int = 4096, retries: int = 3, model_id: str = "") -> str:
    client = boto3.client("bedrock-runtime")
    bedrock_model = model_id or _active_model_id
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
                modelId=bedrock_model,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(response["body"].read())
            stop_reason = result.get("stop_reason", "")
            text = result["content"][0]["text"]
            if stop_reason == "max_tokens":
                print(
                    f"[WARN] _bedrock_call: output truncated (stop_reason=max_tokens, "
                    f"max_tokens={max_tokens}, output_len={len(text)}). "
                    f"Last 80 chars: ...{text[-80:]!r}"
                )
            return text
        except Exception as exc:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("Unreachable")


def _pass1_structure(topic: str, angle: str, context: str, profile: dict, max_attempts: int = 3) -> dict:
    target_min = profile.get("script", {}).get("target_duration_min", 10)
    target_max = profile.get("script", {}).get("target_duration_max", 16)
    tone = profile.get("script", {}).get("tone", "authoritative_compelling")
    narrative = profile.get("script", {}).get("narrative_style", "third_person_omniscient")

    # Human YouTuber persona prompt — writes for ears, not eyes
    prompt = (
        f"You are a human YouTube creator with 10 million subscribers. You write the way you talk. "
        f"Short sentences. Fragments. Rhythm. You never sound like an AI.\n\n"
        f"BANNED PHRASES — never use these words or phrases under any circumstances:\n"
        f"\"In conclusion\", \"It's worth noting\", \"Delve\", \"Fascinating\", \"Moreover\", "
        f"\"Certainly\", \"Absolutely\"\n\n"
        f"═══ ASSIGNMENT ═══\n"
        f"Topic: {topic}\n"
        f"Angle: {angle}\n"
        f"Research context: {context}\n\n"

        f"═══ VOICE & STYLE — WRITE FOR EARS, NOT EYES ═══\n"
        f"- Target duration: {target_min}-{target_max} minutes of narrated content\n"
        f"- Tone: {tone}\n"
        f"- Narrative style: {narrative}\n"
        f"- Write fragments. One idea per sentence. Never write like a textbook.\n"
        f"- Short punchy sentences land harder than long complex ones.\n"
        f"- Use contractions. Use rhetorical questions. Sound human.\n"
        f"- STRUCTURE: Create 8-14 richly detailed scenes (NOT fewer)\n"
        f"- Each scene MUST contain 150-250 words of narration in 'narration_text' (6-10 punchy lines, NOT bullet points)\n"
        f"- Use [PAUSE], [BEAT], [BREATH], [CUT TO] markers for natural pacing\n"
        f"- [PAUSE] = a full stop beat for emphasis\n"
        f"- [BEAT] = a quick pause between ideas\n"
        f"- [BREATH] = a natural breath point\n"
        f"- [CUT TO] = signals a hard visual cut to new footage\n"
        f"- Use vivid but ACCURATE descriptions — paint scenes the listener can see\n"
        f"- Build narrative tension across scenes — each should flow into the next\n"
        f"- Include specific details that demonstrate depth (names, places, mechanisms)\n"
        f"- estimated_duration = round(word_count_in_narration_text / 130 * 60). A 150-word scene = 69s. NEVER output 0.\n"
        f"- total_duration_estimate = sum of all scene estimated_duration values\n"
        f"- nova_canvas_prompt: write a richly descriptive text-to-image prompt for Amazon Nova Canvas. Include subject, setting, lighting quality, photographic style, mood. Be specific and cinematic.\n"
        f"- nova_reel_prompt: describe the camera motion and animation for Amazon Nova Reel. Use terms like 'slow push-in', 'parallax drift', 'orbital pan', '3D ease in/out', 'crane shot rising'. Match the scene's emotion.\n"
        f"- text_overlay: short punchy phrase (max 80 chars) that appears as a typewriter animation. Can be a key quote, stat, or scene title. Leave empty string if not applicable.\n\n"

        f"═══ RE-HOOK EVERY 90 SECONDS ═══\n"
        f"Every ~90 seconds of content (approximately every 1-2 sections) you MUST drop a re-hook:\n"
        f"- A re-hook resets viewer attention and prevents drop-off\n"
        f"- It teases what's coming next: 'And what happens next? Nobody saw it coming.'\n"
        f"- Or it reframes what just happened: 'Think about that for a second. [PAUSE] That changes everything.'\n"
        f"- Or it asks a direct question to the viewer: 'So why did nobody stop this? Keep watching.'\n"
        f"- Place re-hooks at the END of sections where duration_estimate_sec >= 90, or after every 2nd section\n\n"

        f"═══ CINEMATIC QUALITY GUIDELINES ═══\n"
        f"- Open with a cold open / dramatic hook — drop the viewer into the most compelling moment\n"
        f"- Use 'chapter-style' section titles that tease what's coming\n"
        f"- Build rising action across the first 2/3 of the script, then resolve\n"
        f"- Each section should have its own mini arc: setup → tension → revelation\n"
        f"- Use sensory language: what does the scene look, sound, feel like?\n"
        f"- Include moments of silence/breathing room for emotional impact ([BEAT] [PAUSE])\n"
        f"- The final section should deliver a gut-punch ending that reframes everything\n"
        f"- Write MULTIPLE nova_canvas_prompt descriptions per scene — one vivid, cinematic image prompt\n"
        f"- Consider visual variety: wide establishing shots, close-ups, archival style, maps/documents, aerial\n"

        f"═══ RETENTION ARCHITECTURE — OPEN LOOPS ═══\n"
        f"To prevent viewer drop-off, you MUST embed 'Open Loops' every 2-3 sections:\n"
        f"- An Open Loop is a teaser or forward hook that makes the viewer need to keep watching\n"
        f"- Examples: 'But what happened next would change everything...', 'The answer, hidden for decades, is coming up', 'You won't believe what they found when they looked closer'\n"
        f"- Place an Open Loop at the END of sections 2, 4, 6, 8 (and any section before a major revelation)\n"
        f"- Each Open Loop must tease a REAL payoff that arrives 1-2 sections later — never mislead\n"
        f"- Use the CLAIM → INTRIGUE → PAYOFF structure: state a claim, create intrigue, deliver the payoff sections later\n"
        f"- The first section should always end with an Open Loop that sets up the central mystery or question\n\n"

        f"═══ FACTUAL INTEGRITY — ABSOLUTE RULES ═══\n"
        f"NEVER invent, assume, or embellish facts. Before writing ANY claim, ask yourself:\n"
        f"\"Do I know this for certain?\"\n\n"
        f"STRICT RULES:\n"
        f"1. Only include events, dates, names, statistics that are real and well-documented\n"
        f"2. NEVER fill gaps with \"likely\", \"probably\", or assumed details presented as fact\n"
        f"3. If a story sounds interesting but you are not 100% certain it is real — OMIT it entirely\n"
        f"4. Do NOT exaggerate or dramatize facts beyond what is documented\n"
        f"5. Dates, names, and numbers MUST be accurate — if unsure, leave them out\n"
        f"6. Do NOT present speculation as established fact\n"
        f"7. Do NOT conflate separate events or attribute actions to the wrong people\n\n"

        f"WHEN YOU ARE UNCERTAIN:\n"
        f"- If you cannot verify a specific date, number, or name: use [NEEDS SOURCE] as a placeholder\n"
        f"- If a claim is plausible but you are not fully confident: use [UNVERIFIED: \"the claim\"]\n"
        f"- This lets the human editor know to fact-check before publishing\n\n"

        f"QUALITY SELF-CHECK (do this before finalizing):\n"
        f"1. Is every fact in this script something I know with HIGH confidence?\n"
        f"2. Are there any details I \"filled in\" or assumed that I should not have?\n"
        f"3. Would this script hold up to basic fact-checking by a journalist?\n"
        f"4. Did I attribute any quotes, actions, or events to the wrong person/time?\n"
        f"5. Did I accidentally use any banned phrases (In conclusion, Delve, Fascinating, Moreover, Certainly, Absolutely, It's worth noting)?\n"
        f"If the answer to #3 is NO — rewrite those sections before responding.\n\n"

        f"═══ CONTENT DEPTH GUIDELINES ═══\n"
        f"- Open with a hook that grabs attention using a REAL, verifiable fact or question\n"
        f"- Each section should teach the viewer something specific and substantive\n"
        f"- Use concrete examples instead of vague generalizations\n"
        f"- Include cause-and-effect reasoning: explain WHY things happened, not just WHAT\n"
        f"- Add context that helps viewers understand significance\n"
        f"- End with a thought-provoking line that ties back to the hook\n"
        f"- Populate \"source_notes\" for each scene to indicate factual basis\n"
        f"- Set \"factual_confidence\" to honestly reflect your certainty level\n\n"

        f"═══ OUTPUT FORMAT ═══\n"
        f"Return ONLY valid JSON matching this schema (no markdown, no extra text):\n{SCRIPT_JSON_SCHEMA}\n\n"
        f"IMPORTANT: Each scene must have a unique integer scene_id starting at 1, a nova_canvas_prompt, a nova_reel_prompt, and a text_overlay (empty string if none).\n"
        f"CRITICAL: You MUST output complete, valid JSON with all brackets and braces properly closed. "
        f"If the script is getting long, reduce the number of scenes rather than leaving JSON incomplete. "
        f"Always close every {{ with }} and every [ with ]. Double-check your JSON is valid before finishing."
    )
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        raw = _bedrock_call(prompt, max_tokens=32768)
        try:
            result = _extract_json(raw)
            edl_errors = _validate_edl_schema(result)
            if edl_errors:
                print(
                    f"[WARN] _pass1_structure EDL validation failed (attempt {attempt + 1}/{max_attempts}): "
                    f"{edl_errors}"
                )
                last_err = ValueError(f"EDL schema invalid: {edl_errors}")
                time.sleep(2 ** attempt)
                continue
            return result
        except json.JSONDecodeError as exc:
            last_err = exc
            print(
                f"[WARN] _pass1_structure JSON parse failed (attempt {attempt + 1}/{max_attempts}): {exc}\n"
                f"[DEBUG] raw response length={len(raw)}, last 500 chars: ...{raw[-500:]!r}"
            )
            time.sleep(2 ** attempt)
    raise last_err


def _pass2_hook_rewrite(script: dict) -> dict:
    prompt = (
        "You are an expert at writing viral YouTube hooks that are BOTH compelling AND factually honest.\n\n"
        "Rewrite the given hook to be punchy, emotionally gripping, and impossible to click away from.\n\n"
        "RULES:\n"
        "- The hook MUST be grounded in real, verifiable facts from the script\n"
        "- Do NOT invent statistics, quotes, or claims that aren't in the original content\n"
        "- Do NOT use misleading framing that implies something the video doesn't deliver\n"
        "- You CAN use rhetorical questions, dramatic pacing, and emotional language\n"
        "- You CAN highlight the most surprising REAL fact from the script\n"
        "- The hook should make a promise the video actually keeps\n\n"
        "Return ONLY a JSON object (no markdown) with keys 'hook' (string) and 'hook_emotion' "
        "(tense|excited|curious|dramatic).\n\n"
        f"Original hook: {script['hook']}\n"
        f"Video topic: {script['title']}\n"
        f"Mood: {script.get('mood', 'neutral')}\n"
        f"Script summary: {script.get('description', '')}"
    )
    raw = _bedrock_call(prompt, max_tokens=512)
    try:
        rewrite = _extract_json(raw)
        script["hook"] = rewrite.get("hook", script["hook"])
        script["hook_emotion"] = rewrite.get("hook_emotion", script.get("hook_emotion", "curious"))
    except json.JSONDecodeError:
        pass
    return script


def _pass_fact_integrity(script: dict) -> dict:
    """Self-audit pass: review the script for factual integrity.

    Uses Bedrock to re-examine every claim in the script and flag or remove
    anything that cannot be verified. Runs for ALL profiles.
    """
    prompt = (
        "You are a rigorous fact-checker and editorial auditor. Your job is to review "
        "the following YouTube script and ensure EVERY factual claim is accurate.\n\n"

        "═══ YOUR TASK ═══\n"
        "Go through each scene's narration_text line by line and:\n"
        "1. REMOVE any fact, statistic, date, name, or event you are not 100% certain is accurate\n"
        "2. Replace removed claims with [NEEDS SOURCE] if the scene needs that info to make sense\n"
        "3. Flag uncertain claims as [UNVERIFIED: \"the claim\"] — do NOT delete them silently\n"
        "4. Fix any dates, numbers, or names you KNOW are wrong\n"
        "5. Remove any embellishment or dramatization that goes beyond documented facts\n"
        "6. Check that no quotes are misattributed\n"
        "7. Ensure cause-and-effect claims are accurate, not just plausible\n\n"

        "═══ WHAT TO LOOK FOR ═══\n"
        "- Specific statistics without clear origin (e.g. \"73% of people...\")\n"
        "- Precise dates/years that might be off by a year or more\n"
        "- Named individuals doing things they might not have actually done\n"
        "- Events described with details that sound dramatized\n"
        "- \"Likely\" or \"probably\" presented as established fact\n"
        "- Conflation of separate events into one narrative\n\n"

        "═══ RULES ═══\n"
        "- Do NOT add new content or expand the script\n"
        "- Do NOT change the tone, style, or structure\n"
        "- Do NOT modify visual_cue fields\n"
        "- ONLY modify \"narration_text\", \"source_notes\", and \"factual_confidence\" fields\n"
        "- Update \"source_notes\" for each scene to reflect your assessment\n"
        "- Set the top-level \"factual_confidence\" to \"high\", \"medium\", or \"low\"\n"
        "- Keep [PAUSE], [BEAT], [BREATH] markers exactly where they are\n"
        "- Return the COMPLETE script as valid JSON, same schema\n\n"

        "CRITICAL: Output complete, valid JSON with all brackets and braces properly closed.\n\n"
        f"{json.dumps(script, indent=2)}"
    )
    raw = _bedrock_call(prompt, max_tokens=32768)
    try:
        audited = _extract_json(raw)
        audited.setdefault("factual_confidence", "medium")
        orig_scenes = script.get("scenes", [])
        new_scenes = audited.get("scenes", [])
        if (
            not new_scenes
            or not isinstance(new_scenes, list)
            or not all(isinstance(s, dict) for s in new_scenes)
        ):
            print(
                f"[WARN] _pass_fact_integrity: scenes corrupted "
                f"(got {type(new_scenes).__name__} with "
                f"{sum(1 for s in new_scenes if not isinstance(s, dict)) if isinstance(new_scenes, list) else '?'} "
                f"non-dict items) — keeping original {len(orig_scenes)} scenes"
            )
            audited["scenes"] = orig_scenes
        return audited
    except json.JSONDecodeError:
        script.setdefault("factual_confidence", "unaudited")
        return script


def _pass3_visual_cues(script: dict, profile: dict) -> dict:
    color_grade = profile.get("visuals", {}).get("color_grade_default", "cinematic_warm")
    transition = profile.get("editing", {}).get("default_transition", "dissolve")

    for i, scene in enumerate(script.get("scenes", [])):
        if not isinstance(scene, dict):
            print(f"[WARN] _pass3_visual_cues: scene {i} is {type(scene).__name__}, skipping")
            continue
        prompt = (
            f"Generate precise visual cue metadata for this documentary scene.\n"
            f"Scene title: {scene.get('title', '')}\n"
            f"Narration excerpt: {scene.get('narration_text', '')[:500]}\n"
            f"Emotion: {scene.get('emotion', 'neutral')}\n"
            f"Scene duration: {scene.get('estimated_duration', 60)}s\n"
            f"Nova Canvas prompt (base image): {scene.get('nova_canvas_prompt', '')}\n"
            f"Nova Reel prompt (camera motion): {scene.get('nova_reel_prompt', '')}\n"
            f"Default color grade: {color_grade}\n"
            f"Default transition: {transition}\n\n"
            "Return ONLY valid JSON (no markdown) with this structure:\n"
            "{\n"
            '  "camera_style": "ken_burns_in|ken_burns_out|pan_left|pan_right|static|slow_drift|dolly_in",\n'
            f'  "color_grade": "{color_grade}",\n'
            '  "transition_in": "crossfade|cut|zoom_punch|whip|dissolve|fade_black|wipeleft",\n'
            '  "overlay_type": "none|lower_third|stat_counter|quote_card"\n'
            "}\n"
        )
        raw = _bedrock_call(prompt, max_tokens=256)
        try:
            cue = _extract_json(raw)
        except json.JSONDecodeError:
            cue = {
                "camera_style": "static",
                "color_grade": color_grade,
                "transition_in": transition,
                "overlay_type": "none",
            }
        script["scenes"][i]["visual_cue"] = cue
    return script


def _pass4_pacing(script: dict, profile: dict) -> dict:
    cpm = profile.get("editing", {}).get("cuts_per_minute_target", 8)
    prompt = (
        f"Polish the pacing of this YouTube documentary script for {cpm} cuts per minute. "
        "Adjust [PAUSE], [BEAT], [BREATH] markers, tighten sentences, and update "
        "estimated_duration for each scene. Return the full script JSON "
        "(same schema, no markdown changes, only values updated).\n\n"
        "RULES:\n"
        "- Do NOT add new factual claims, statistics, or details that weren't in the original\n"
        "- Do NOT remove [NEEDS SOURCE] or [UNVERIFIED] markers\n"
        "- Do NOT change the meaning of any sentence — only tighten the wording\n"
        "- Preserve source_notes, factual_confidence, nova_canvas_prompt, nova_reel_prompt, text_overlay fields unchanged\n"
        "- CRITICAL: Output complete, valid JSON with all brackets and braces properly closed.\n\n"
        f"{json.dumps(script, indent=2)}"
    )
    raw = _bedrock_call(prompt, max_tokens=32768)
    try:
        paced = _extract_json(raw)
        orig_scenes = script.get("scenes", [])
        new_scenes = paced.get("scenes", [])
        if (
            not new_scenes
            or not isinstance(new_scenes, list)
            or not all(isinstance(s, dict) for s in new_scenes)
        ):
            print("[WARN] _pass4_pacing: scenes corrupted — keeping originals")
            paced["scenes"] = orig_scenes
        return paced
    except json.JSONDecodeError:
        return script


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
                    "You are a rigorous fact-checker with access to live web search. "
                    "Your job is to verify every factual claim in this script against real sources.\n\n"
                    "FOR EACH CLAIM:\n"
                    "- If VERIFIED: keep it and update source_notes with your source\n"
                    "- If INCORRECT: fix it with the correct data and note the correction in source_notes\n"
                    "- If UNVERIFIABLE: mark it as [UNVERIFIED: \"the claim\"] in the narration_text\n"
                    "- If FABRICATED (no evidence it ever happened): REMOVE it entirely and replace with [NEEDS SOURCE]\n\n"
                    "RULES:\n"
                    "- Do NOT add new fabricated facts to replace removed ones\n"
                    "- Do NOT invent sources or citations\n"
                    "- Do NOT present your corrections with false confidence if you cannot verify them either\n"
                    "- Update factual_confidence to reflect your overall assessment\n"
                    "- Return ONLY the updated script as valid JSON, same schema."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Fact-check and verify this script. Search the web for each major claim:\n"
                    f"{json.dumps(script, indent=2)}"
                ),
            },
        ],
        "max_tokens": 8192,
    }
    result = _http_post(
        "https://api.perplexity.ai/chat/completions", headers=headers, body=body
    )
    raw = result["choices"][0]["message"]["content"].strip()
    try:
        return _extract_json(raw)
    except json.JSONDecodeError:
        return script


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


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    topic: str = event["selected_topic"]
    angle: str = event["angle"]
    trending_context: str = event.get("trending_context", "")
    dry_run: bool = event.get("dry_run", False)

    step_start = notify_step_start("script", run_id, niche=event.get("niche", ""), profile=profile_name, dry_run=dry_run)

    try:
        s3 = boto3.client("s3")
        log.info("Loading profile: %s", profile_name)
        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        # Use the model configured in the profile (falls back to default)
        global _active_model_id
        _active_model_id = profile.get("llm", {}).get("script_model", BEDROCK_MODEL_ID_DEFAULT)
        log.info("Using Bedrock model: %s", _active_model_id)

        if dry_run:
            log.info("DRY RUN mode — returning stub script")
            script = {
                "title": f"[DRY RUN] {topic}",
                "description": "Dry run script.",
                "tags": ["dry_run"],
                "hook": "This is a dry run.",
                "hook_emotion": "neutral",
                "scenes": [
                    {
                        "scene_id": 1,
                        "title": "Scene 1",
                        "narration_text": "Dry run content. [PAUSE]",
                        "nova_canvas_prompt": "Cinematic wide shot of an empty studio, neutral lighting",
                        "nova_reel_prompt": "Slow push-in, static camera, subtle depth of field",
                        "text_overlay": "",
                        "estimated_duration": 60,
                        "emotion": "neutral",
                        "visual_cue": {
                            "camera_style": "static",
                            "color_grade": "cinematic_warm",
                            "transition_in": "dissolve",
                            "overlay_type": "none",
                        },
                    }
                ],
                "cta": "Subscribe for more.",
                "total_duration_estimate": 60,
                "mood": "neutral",
            }
        else:
            perplexity_key = get_secret("nexus/perplexity_api_key")["api_key"]

            log.info("Pass 1/6: Generating script structure for '%s'", topic)
            script = _pass1_structure(topic, angle, trending_context, profile)

            log.info("Pass 2/6: Fact integrity self-audit")
            script = _pass_fact_integrity(script)

            log.info("Pass 3/6: Hook rewrite")
            script = _pass2_hook_rewrite(script)

            log.info("Pass 4/6: Visual cues")
            script = _pass3_visual_cues(script, profile)

            log.info("Pass 5/6: Pacing polish")
            script = _pass4_pacing(script, profile)

            log.info("Pass 6/6: Perplexity fact-check (web-verified)")
            script = _pass5_fact_check(script, perplexity_key)

            confidence = script.get("factual_confidence", "unknown")
            log.info("Script complete — factual_confidence=%s", confidence)
        scenes = script.get("scenes", [])
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            if not scene.get("estimated_duration"):
                words = len(scene.get("narration_text", "").split())
                scene["estimated_duration"] = max(30, int(words / 130 * 60))
        total_dur = script.get("total_duration_estimate") or 0
        if not total_dur:
            total_dur = sum(float(s.get("estimated_duration", 0) if isinstance(s, dict) else 0) for s in scenes)
        script["total_duration_estimate"] = int(total_dur)
        log.info("Duration: %d scenes, total_duration_estimate=%ds", len(scenes), int(total_dur))
        script["run_id"] = run_id
        log.info("Saving script to S3")
        s3_key = _save_to_s3(run_id, script)

        elapsed = time.time() - step_start
        notify_step_complete("script", run_id, [
            {"name": "Title", "value": script["title"][:100], "inline": False},
            {"name": "Scenes", "value": str(len(script.get("scenes", []))), "inline": True},
            {"name": "Est. Duration", "value": f"{script.get('total_duration_estimate', 0)}s", "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
            {"name": "Fact Confidence", "value": script.get("factual_confidence", "N/A"), "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0x9B59B6)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": dry_run,
            "script_s3_key": s3_key,
            "title": script["title"],
            "description": script.get("description", ""),
            "tags": script.get("tags", []),
            "total_duration_estimate": script.get("total_duration_estimate", 0),
            "scene_count": len(script.get("scenes", [])),
            "factual_confidence": script.get("factual_confidence", "unknown"),
        }

    except Exception as exc:
        log.error("Script step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "script", exc)
        raise
