# PHASE 3 COMPLETE — True Crime Visual Enhancement

## Summary

Phase 3 upgrades the visual pipeline, editor captions, and thumbnail generation
for the True Crime genre profile. All tasks were implemented without breaking
existing pipeline logic.

---

## Files Changed

### 1. `lambdas/nexus-visuals/handler.py`

**Why:** Task 3.1 — Pexels-first dark visual strategy for True Crime.

**Changes:**
- Added `import urllib.parse` and `import urllib.request` at module level
- Added `_TRUE_CRIME_BOOST_LABELS` set: `person`, `night`, `building`, `road`, `vehicle`,
  `darkness`, `shadow`, `forest` — used to boost Rekognition scores for True Crime
- Added `_TRUE_CRIME_PENALIZE_LABELS` set: `beach`, `flowers`, `sunshine`, `party`, `food` —
  used to penalize Rekognition scores for True Crime
- Modified `_rekognition_score()`: now accepts optional `profile` parameter; when
  `profile["script"]["style"] == "true_crime"`, applies boost (+0.1 per dark label) and
  penalty (-0.15 per bright label), clamped to [0.0, 1.0]
- Modified `_select_best_candidate()`: passes `profile` through to `_rekognition_score()`
- Added `_fetch_pexels_video()`: fetches landscape video from Pexels API using
  profile keywords + scene-specific keywords; filters by orientation=landscape,
  size=large, min_duration=5s; uses Authorization header without "Bearer" prefix
- Added `_fetch_pexels_photo()`: fetches landscape photo from Pexels photo API;
  returns raw image bytes for Ken Burns processing in editor
- Added `_generate_dark_gradient_video()`: generates dark gradient fallback video
  using FFmpeg `gradients` filter with timeout=60s
- Rewrote `_process_scene()`: now accepts `profile` parameter and implements the new
  5-tier priority order:
  1. Pexels video (landscape, size=large, min_duration=5s) — uses profile pexels_keywords
     + scene description keywords
  2. Pexels photo + Ken Burns flag (`clip_type="static_image"` in EDL)
  3. Nova Canvas dark atmospheric image + Ken Burns flag (for True Crime: adds dark suffix
     to prompt; falls through to static_image)
  4. Nova Reel (only when `profile["visuals"]["avoid_nova_reel"] is False`)
  5. Static image fallback (editor applies Ken Burns)
- Modified `lambda_handler()`: passes `profile` dict to `_process_scene()`; dry-run
  path now includes `clip_type: "video"` in scene output

**Verification:**
- `avoid_nova_reel: true` in `profiles/true_crime.json` → Nova Reel (Tier 4) never called
- All Pexels API calls use `Authorization: <key>` header (no "Bearer" prefix)
- Static image clips flagged with `clip_type: "static_image"` in EDL output

---

### 2. `lambdas/nexus-editor/handler.py`

**Why:** Task 3.2 — Captions burn-in and Ken Burns effect for static_image clips.

**Changes (all additive — existing FFmpeg assembly logic unchanged):**
- Added `_apply_ken_burns()`: applies FFmpeg `zoompan` filter for slow zoom-in
  (scale 1.0 → 1.05 over full duration) to static image files; falls back to static
  frame if zoompan fails; accepts `width`, `height`, `duration`, `tmpdir`, `idx` args;
  `timeout=120` on subprocess call
- Added `_TENSE_EMOTIONS` set: `urgent`, `revelation`, `tense` — for True Crime
  all-caps caption style
- Added `_CAPTION_MAX_WORDS = 6` constant — max words per caption frame
- Added `_load_word_timestamps()`: loads `word_timestamps.json` from
  `s3://nexus-outputs/{run_id}/audio/word_timestamps.json`; returns `None` if not found
  (never raises)
- Added `_build_captions_drawtext()`: converts word timestamps to FFmpeg `drawtext`
  filter strings; groups words into chunks of ≤6; renders base layer (white) +
  per-word highlight layer (yellow); True Crime tense/urgent/revelation emotions →
  all-caps; uses `fontsize=52`, `x=(w-text_w)/2`, `y=h-120`, `box=1`, `boxcolor=black@0.5`
- Added `_apply_captions()`: applies captions as a separate FFmpeg pass after
  assembly (`-c:v libx264 -crf 18 -c:a copy`); on failure: logs error to
  `s3://nexus-outputs/{run_id}/errors/editor.json` and returns original assembled path;
  `timeout=3600`
- Modified clip processing loop in `lambda_handler()`: when `sec.get("clip_type") ==
  "static_image"`, calls `_apply_ken_burns()` before the loop/extend step
- Modified post-mux in `lambda_handler()`: after `final_local` is written:
  1. Loads word timestamps via `_load_word_timestamps()`
  2. If found: applies `_apply_captions()` (non-fatal)
  3. If not found: logs `logger.warning("[{run_id}] editor: no word timestamps found, skipping captions")`

**Verification:**
- Captions skip gracefully when `word_timestamps.json` is absent ✅
- Ken Burns only applied when `clip_type == "static_image"` ✅
- FFmpeg captions pass has `timeout=3600` ✅
- Ken Burns pass has `timeout=120` ✅
- EFS `/mnt/scratch/{run_id}/` cleanup in `finally` block unchanged ✅

---

### 3. `lambdas/nexus-thumbnail/handler.py`

**Why:** Task 3.3 — True Crime thumbnail prompts with 3 variant angles.

**Changes:**
- Added `_TRUE_CRIME_NEGATIVE_PROMPT` constant: excludes `"bright colors, cartoon,
  illustration, anime, cheerful, sunny"` plus standard watermark/quality exclusions
- Added `_TRUE_CRIME_VARIANT_ANGLES` list: defines 3 variant angles:
  - `victim`: empathy angle, candid portrait, dramatic lighting
  - `evidence`: crime scene evidence, mysterious location, forensic detail
  - `suspect`: dark silhouette, shadowed face, tension angle
- Added `_generate_true_crime_thumbnail_concepts()`: generates 3 True Crime concepts
  using the Netflix documentary aesthetic prompt template (no Bedrock call needed —
  prompts are deterministic); sets `angle` key on each concept
- Modified `_generate_nova_canvas_background()`: now accepts optional `negative_prompt`
  parameter (defaults to standard exclusions)
- Added `_render_true_crime_thumbnail()`: renders a True Crime thumbnail variant with:
  - Top 80%: Nova Canvas dark atmospheric image (with `_TRUE_CRIME_NEGATIVE_PROMPT`)
  - Bottom 20%: dark gradient overlay via Pillow
  - Title text: `script["title"]` in bold white uppercase (font size 64)
  - Subtext: cliffhanger line from Act 5 (last sentence) or concept overlay_text
  - Red accent bar (RGB 220,20,20) under the title text
  - Dark fallback if Nova Canvas fails
- Modified `lambda_handler()`: detects `profile["script"]["style"] == "true_crime"`:
  - True: uses `_generate_true_crime_thumbnail_concepts()` + `_render_true_crime_thumbnail()`
  - False: unchanged standard path with `_generate_thumbnail_concepts()` + `_render_thumbnail()`

**Verification:**
- True Crime prompt only activates when `profile["script"]["style"] == "true_crime"` ✅
- Standard path unmodified for all other profiles ✅
- 3 variants always generated ✅

---

### 4. `scripts/tests/test_visuals_handler.py`

**Why:** Task 3.4 — Tests for Phase 3 visuals changes.

**New test classes:**
- `TestRekognitionScoreTrueCrime`: 3 tests for boosting/penalizing dark/bright labels
- `TestAvoidNovaReel`: 3 tests verifying `avoid_nova_reel` flag is respected and
  Pexels functions return None on missing API key

---

### 5. `scripts/tests/test_thumbnail_handler.py`

**Why:** Task 3.4 — Tests for Phase 3 thumbnail changes.

**New test class `TestTrueCrimeThumbnails`:** 6 tests covering:
- 3-variant concept generation with correct angles
- Dark prompt content validation
- All-caps overlay text for True Crime
- Negative prompt excludes bright elements
- `lambda_handler` routes to True Crime path for `true_crime` profile
- `lambda_handler` routes to standard path for non-True Crime profiles

---

### 6. `scripts/tests/test_editor_phase3.py` (new file)

**Why:** Task 3.4 — Tests for Phase 3 editor features.

**Test classes:**
- `TestNewFunctionsExist`: confirms all 4 new editor functions are callable
- `TestLoadWordTimestamps`: 3 tests (happy path, S3 miss, malformed JSON)
- `TestBuildCaptionsDrawtext`: 7 tests (empty, single word, True Crime all-caps,
  neutral case preservation, max-6-words chunking, non-True-Crime never uppercases,
  lower-third position, font size)
- `TestApplyCaptionsSkipsGracefully`: 2 tests (empty timestamps, FFmpeg failure fallback)

---

## Task 3.4 Verification Checklist

| Check | Status |
|---|---|
| `avoid_nova_reel` flag respected — Nova Reel never called for true_crime | ✅ |
| Captions skip gracefully if `word_timestamps.json` missing | ✅ |
| Ken Burns only applied to `static_image` flagged clips | ✅ |
| Thumbnail prompts only activate when `profile["script"]["style"] == "true_crime"` | ✅ |
| All FFmpeg commands have timeouts (`timeout=` kwarg on `subprocess.run`) | ✅ |
| EFS `/mnt/scratch/{run_id}/` cleanup in `finally` block | ✅ |
| `python3 -m pytest scripts/tests/ -q --tb=short` | **379 passed, 2 skipped** ✅ |
