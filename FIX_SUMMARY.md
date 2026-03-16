# Script Lambda Fix Summary

**Date:** March 15, 2026  
**Run ID:** 9450f8e9-62d9-4123-8a15-85721b391667  
**Issue:** Script step failed with EDL schema validation errors on scene 9

## Problem

The Script Lambda was failing during `_pass1_structure` with the following error:

```
ValueError: EDL schema invalid: [
  "scenes[9] missing required field 'nova_canvas_prompt'",
  "scenes[9] missing required field 'nova_reel_prompt'",
  "scenes[9] missing required field 'text_overlay'",
  "scenes[9] missing required field 'estimated_duration'",
  "scenes[9].estimated_duration must be a positive number"
]
```

**Root Cause:** Claude Sonnet was hitting the 6000 token output limit during script generation, causing scene 9 to be truncated and missing required fields. The existing JSON repair logic could close brackets/braces but couldn't fill in missing semantic fields.

## Solution Implemented

### 1. Increased Token Budget
- Increased `max_tokens` from **6000 → 8000** in all script generation passes:
  - `_pass1_structure`: 6000 → 8000
  - `_pass_fact_integrity`: 6000 → 8000
  - `_pass4_pacing`: 6000 → 8000
  - `_pass6_final_polish`: 6000 → 8000

### 2. Added Validation Feedback Loop
Enhanced `_pass1_structure` to include validation errors from previous attempts in the retry prompt:

```python
if validation_feedback and attempt > 0:
    retry_prompt = (
        f"{prompt}\n\n"
        f"⚠️ PREVIOUS ATTEMPT HAD THESE ERRORS — FIX THEM:\n{validation_feedback}\n"
        f"Make sure EVERY scene has all required fields: scene_id, narration_text, "
        f"nova_canvas_prompt, nova_reel_prompt, text_overlay, estimated_duration.\n"
        f"If you're running out of space, reduce the number of scenes instead of leaving them incomplete."
    )
```

### 3. Added Auto-Fill Fallback
Created `_autofill_missing_scene_fields()` function that salvages incomplete scenes by:

- **scene_id**: Auto-increments from index (1-based)
- **nova_canvas_prompt**: Derives from narration text or scene title
- **nova_reel_prompt**: Uses default cinematic camera movement
- **text_overlay**: Defaults to empty string
- **estimated_duration**: Calculates from word count (~150 words/min speaking rate)

This function runs after schema validation fails, attempting to rescue incomplete output before giving up.

## Files Modified

1. **`lambdas/nexus-script/handler.py`**
   - Added `_autofill_missing_scene_fields()` function (lines 351-402)
   - Enhanced `_pass1_structure()` with validation feedback loop (lines 537-562)
   - Increased all `max_tokens` parameters to 8000

2. **`scripts/tests/test_script_handler.py`**
   - Added `test_autofill_missing_scene_fields()` test case

## Test Results

All 8 tests pass:
```
✅ test_autofill_missing_scene_fields
✅ test_five_pass_script_generation_calls_bedrock
✅ test_handler_returns_error_if_all_repair_attempts_fail
✅ test_pass_6_uses_opus_model
✅ test_passes_1_to_5_use_sonnet_model
✅ test_repair_truncated_json_handles_broken_json
✅ test_repair_truncated_json_handles_complete_json
✅ test_system_prompt_has_cache_control
```

## Deployment Status

✅ Lambda function `nexus-script` successfully updated in us-east-1  
✅ LastUpdateStatus: Successful

## Expected Behavior

The Script Lambda will now:

1. **First attempt**: Try to generate a valid script with 8000 token budget
2. **On validation failure**: Include specific errors in retry prompt to help Claude fix them
3. **On retry exhaustion**: Attempt to auto-fill missing fields before failing
4. **Result**: Much higher success rate for complex scripts with 10+ scenes

## Retry Strategy

The fix implements a 3-tier recovery strategy:

1. **Retry with feedback** (attempts 1-3): Tell Claude what to fix
2. **Auto-fill recovery**: Populate missing fields with reasonable defaults
3. **Re-validate**: Check if auto-fill resolved all schema errors
4. **Final validation**: Only raise error if auto-fill + re-validation still fails

This ensures we salvage as much valid work as possible instead of discarding a mostly-complete script.

## Known Limitations

- Auto-filled prompts may be less creative than Claude-generated ones
- Estimated duration from word count is approximate (~±15s variance)
- Auto-fill only works for truncated scenes that have at least `narration_text` — if even that is missing, the scene is likely too corrupted to salvage

## Recommendations

For future runs:
- Monitor CloudWatch logs for `[INFO] _pass1_structure: auto-filled missing fields` messages
- If auto-fill triggers frequently, consider further increasing token budget to 10000
- Consider adding scene count limits in prompts (e.g., "Generate 8-12 scenes maximum")

---

**Status:** ✅ DEPLOYED AND TESTED

