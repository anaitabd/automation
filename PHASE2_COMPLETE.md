# PHASE 2 COMPLETE — True Crime Audio Enhancement

## Summary

Phase 2 upgrades the Nexus Cloud pipeline to produce audio and scripts that feel like a
professional True Crime YouTube channel. All changes are production-safe and conditionally
activated by `profile["script"]["style"] == "true_crime"` where applicable.

---

## Files Changed

### 1. `lambdas/nexus-audio/handler.py`

**Why:** Core audio handler required ElevenLabs fail-fast, Polly Neural upgrade,
True Crime emotion detection, and silence injection.

**Changes:**
- Added `ELEVENLABS_QUOTA_EXHAUSTED = False` module-level flag (resets on cold start)
- Updated `_synthesize_sentence()` to try ElevenLabs exactly once with `timeout=8s` (no retries)
- Updated `_should_fallback_to_polly()` to also detect `"limit_reached"` in response body
- Rewrote `_synthesize_sentence_with_fallback()` with proper 3-tier cascade:
  - Tier 1 (ElevenLabs): skipped if quota flag is True; single try with 8s timeout
  - Tier 2 (Polly Neural): primary fallback; logs warning with reason
  - Tier 3 (Polly Standard): guaranteed fallback; only fires if Neural fails
- Added 5 new True Crime emotions to `SSML_EMOTION_MAP`: `whispering`, `urgent`,
  `revelation`, `dark`, `suspenseful`
- Added `dark_tension` to `MUSIC_MOOD_KEYWORDS` and `true_crime` to `POLLY_VOICE_MAP`
- Added `_apply_punctuation_pauses()`: converts `...` → 700ms break, ` — ` → 400ms,
  ` - ` → 300ms
- Updated `_build_ssml()` with breath effect (`<amazon:breath duration="short" volume="soft"/>`)
  and punctuation pause conversion
- Added `detect_emotion(sentence: str) -> str` True Crime function with 7 priority rules;
  always returns a valid `SSML_EMOTION_MAP` key (default: `"tense"`)
- Updated `_generate_voiceover()` to:
  - Use `detect_emotion()` for True Crime profiles, `_detect_emotion()` for others
  - Validate scene emotion against `SSML_EMOTION_MAP`
  - Generate `silence_800ms` and inject it after `"revelation"` or `"whispering"` scenes

### 2. `lambdas/nexus-shorts/voiceover_generator.py`

**Why:** Short-form voiceover generator needed the same fail-fast ElevenLabs logic,
True Crime emotion support, and silence injection.

**Changes:**
- Added `ELEVENLABS_QUOTA_EXHAUSTED = False` module-level flag
- Added `ELEVENLABS_MODEL = "eleven_multilingual_v2"`
- Added `_elevenlabs_tts_once()` for single-try with 8s timeout
- Added `_extract_tts_error()` and updated `_should_fallback_to_polly()` to detect
  `"limit_reached"` in response body
- Updated `_get_polly_voice_id()` to also check top-level `profile["polly_voice_id"]`
  (matching AGENTS.md schema)
- Added `_apply_punctuation_pauses()` and updated `_build_ssml()` with breath effect
- Added 5 new True Crime emotions to `SSML_EMOTION_MAP`
- Added `detect_emotion(sentence: str) -> str` True Crime function (identical 7-rule logic)
- Added `LanguageCode="en-US"` to Polly Neural synthesis call
- Rewrote `generate_voiceover()` with full 3-tier cascade (ElevenLabs → Neural → Standard)
- Added `_make_silence_mp3()` helper and silence injection after `revelation`/`whispering`
- Function signature extended with `emotion: str = "neutral"` parameter (backward compatible)

### 3. `lambdas/nexus_pipeline_utils.py`

**Why:** AGENTS.md requires shared utilities to be the source of truth for constants
used across handlers.

**Changes:**
- Added canonical `EMOTION_SSML` dict with all 12 emotions (7 original + 5 True Crime)

### 4. `lambdas/nexus-script/handler.py`

**Why:** Script generation passes needed True Crime-specific prompt injections to produce
professionally structured and emotionally tagged True Crime content.

**Changes:**
- `_pass1_structure()`: Injects 6-act structure requirement when `style == "true_crime"`:
  Act 1 (Cold open) → Act 2 (Victim) → Act 3 (Crime) → Act 4 (Investigation) →
  Act 5 (Reveal) → Act 6 (Reflection); also enforces present tense for crime scenes,
  sensory openings, and True Crime-specific emotion tags
- `_pass_fact_integrity()`: Now accepts optional `profile` parameter; injects True Crime
  writing rules when `style == "true_crime"` (sensory details, no narrator introductions,
  paragraph hooks, tense consistency)
- `_pass6_final_polish()`: Now accepts optional `profile` parameter; injects True Crime
  final polish rules when `style == "true_crime"` including mandatory per-scene emotion
  field assignment (Opus model per AGENTS.md — unchanged)
- `lambda_handler()`: Updated calls to `_pass_fact_integrity(script, profile)` and
  `_pass6_final_polish(script, profile)` to pass profile context

### 5. `profiles/true_crime.json`

**Why:** Required by Task 2.4 — new profile for True Crime niche content.

**New file with:**
- ElevenLabs voice settings: stability=0.55, similarity_boost=0.80, style=0.65
- Polly primary voice: Gregory; fallback voice: Matthew
- Script style: `"true_crime"`, 12–18 min target, 6-act structure, default emotion "tense"
- Editing: 3 cuts/min, dark_cinematic color grade
- Visuals: 13 Pexels search keywords for True Crime aesthetics, avoid_nova_reel=true
- Audio: dark_tension music, SFX enabled, silence injection enabled, -14 LUFS target

### 6. `scripts/tests/test_audio_handler.py`

**Why:** Test isolation required module-level flag reset; new features required new tests.

**Changes:**
- Added `reset_elevenlabs_quota_flag` autouse pytest fixture to reset
  `ELEVENLABS_QUOTA_EXHAUSTED` before each test (fixes pre-existing test isolation issue)
- Updated `test_polly_neural_ssml_emotion_mapping` to cover all 12 emotions including
  the 5 new True Crime ones, and verify the breath element is present in SSML
- Added `TestDetectEmotionTrueCrime` class: 17 tests covering all 7 detection rules
  and verifying all results are valid `SSML_EMOTION_MAP` keys
- Added `TestElevenLabsQuotaFlag` class: 4 tests verifying flag starts False, is set
  on quota errors, causes ElevenLabs to be skipped, and triggers on limit_reached
- Added `TestPunctuationPauses` class: 4 tests for punctuation→SSML break conversion
- Added `TestTrueCrimeEmotions` class: 7 tests confirming all 5 new emotions exist
  and all 7 original emotions are preserved

---

## Verification Checklist (Task 2.5)

1. ✅ `ELEVENLABS_QUOTA_EXHAUSTED` resets to `False` on module load (both handler files)
2. ✅ `detect_emotion()` can never return a key not in `SSML_EMOTION_MAP` — default is
   `"tense"` and all 7 rules return hardcoded valid keys
3. ✅ All 5 new emotion keys present in `SSML_EMOTION_MAP`: whispering, urgent, revelation,
   dark, suspenseful (in both handler files and nexus_pipeline_utils.py)
4. ✅ Script pass 6 still uses Opus model (`BEDROCK_MODEL_OPUS = "us.anthropic.claude-opus-4-5-20251101-v1:0"`)
5. ✅ True Crime prompts only activate when `profile["script"]["style"] == "true_crime"`
6. ✅ `python3 -m pytest scripts/tests/ -q --tb=short` — 350 passed, 2 skipped, 0 failures

---

## Security

No new vulnerabilities introduced. CodeQL scan: 0 alerts.
All secrets continue to be read from AWS Secrets Manager only.
No `os.environ` secret access added.
