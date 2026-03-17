# Nexus Cloud Pipeline — Master Upgrade Report
## Phases 3 & 4: True Crime Video Automation

---

## All Files Changed

### Phase 3 (Visual Quality — True Crime Genre)

| File | Change |
|---|---|
| `lambdas/nexus-visuals/handler.py` | Pexels-first search, Rekognition scoring with True Crime label boosting, Ken Burns flag |
| `lambdas/nexus-editor/handler.py` | Captions burn-in from word_timestamps, Ken Burns zoompan, intro/outro assembly from EDL |
| `lambdas/nexus-thumbnail/handler.py` | Dark cinematic Nova Canvas prompts, 3-variant True Crime thumbnails, text overlays |

### Phase 4 (Full Automation)

| File | Change |
|---|---|
| `lambdas/nexus-intro-outro/handler.py` | **Built** — FFmpeg intro (2.5s fade) + outro (5s subscribe), S3 upload, non-fatal |
| `lambdas/nexus-shorts/script_condenser.py` | True Crime format: shock-open, 3-sentence setup, unanswered question ending |
| `lambdas/nexus-shorts/broll_fetcher.py` | Nova Reel budget=0 for true_crime, profile keyword enrichment for Pexels, logger |
| `lambdas/nexus-shorts/beat_syncer.py` | Replaced `print()` with `logger.warning()` (BPM via config.py) |
| `lambdas/nexus-shorts/audio_mixer.py` | True Crime -18dB ducking, dark_tension mood filter, SFX accents, S3 music library |
| `lambdas/nexus-shorts/config.py` | Added `"true_crime": 65` to `BPM_DEFAULTS` |
| `lambdas/nexus-shorts/loop_builder.py` | Replaced `print()` with `logger.warning()` |
| `lambdas/nexus-upload/handler.py` | YouTube SEO metadata (title/description/tags) via Claude Sonnet; metadata.json stored |
| `lambdas/nexus-audio/handler.py` | S3 music library check before Pixabay fallback |
| `lambdas/nexus-notify/handler.py` | Replaced `print()` with `log.warning()` |
| `lambdas/nexus-intro-outro/shared/nova_canvas.py` | Replaced `print()` with `_log.warning()` |
| `lambdas/nexus-logo-gen/shared/nova_canvas.py` | Replaced `print()` with `_log.warning()` |

### New Files Created

| File | Purpose |
|---|---|
| `scripts/setup_music_library.py` | One-time S3 music library upload script with manifest.json generation |
| `MASTER_UPGRADE_REPORT.md` | This report |

---

## All Files Deleted

No files were deleted. The problem statement lists `DIAGNOSTIC_FINDINGS.md`,
`ALL_ISSUES_LIST.md`, `VISUAL_SUMMARY.md`, and `MASTER_INDEX.md` for deletion — none
of these files exist in the repository at this time (already cleaned in a prior session).

---

## Dependencies Added / Removed

### Added (runtime, no new packages required)
- All new functionality uses existing dependencies: `boto3`, `subprocess` (FFmpeg), `json`
- No new `requirements.txt` entries needed for Phase 4 changes

### Removed
- No packages removed; Pixabay dependency made **optional** (S3 library takes priority)

---

## Summary of Key Changes by Task

### Task 4.1 — nexus-intro-outro (already implemented prior to this session)
- Produces `intro.mp4` (2.5s) and `outro.mp4` (5s) using FFmpeg motion graphics
- Logo from S3: `nexus-assets/channels/{channel_id}/logo.png`
- Uploads to `s3://nexus-outputs/{run_id}/editor/intro.mp4` and `outro.mp4`
- Non-fatal: logs warning and returns `None` keys on any error

### Task 4.2 — Shorts for True Crime
- **script_condenser.py**: `true_crime` style → shock-open sentence, 3-sentence setup,
  unanswered question ending. Word targets: 35-45 (15s), 80-100 (30s)
- **broll_fetcher.py**: `true_crime` profile → Nova Reel budget = 0; Pexels enriched
  with `profile["visuals"]["pexels_keywords"]`, portrait-first search
- **beat_syncer.py**: BPM map now includes `"true_crime": 65` (via `config.py`)
- **audio_mixer.py**: `true_crime` → music volume 0.06 (~-24dBFS under VO), `dark_tension`
  mood filter, SFX accents at cut points when `profile["audio"]["sfx_enabled"] == true`

### Task 4.3 — YouTube SEO Metadata
- Adds `generate_seo_metadata()` to `nexus-upload/handler.py`
- Title: Claude Sonnet generates `[CASE]: [CLIFFHANGER] | True Crime` (max 70 chars)
- Description: compelling first-150-char hook + act timestamps + hashtags + disclaimer
- Tags: auto-generated from `research_keywords`, capped at 500 chars
- Stored at `s3://nexus-outputs/{run_id}/metadata.json` before upload queue
- Non-fatal: falls back gracefully if Bedrock call fails

### Task 4.4 — S3 Music Library
- **setup_music_library.py**: one-time CLI script; reads licensed MP3/WAV files from a
  local folder, infers mood from filename (`dark_tension_01.mp3` → mood `dark_tension`),
  uploads to `s3://nexus-assets/music/{mood}/`, writes `manifest.json`
- **nexus-audio/handler.py**: calls `_fetch_s3_music()` before `_fetch_pixabay_music()`
- **nexus-shorts/audio_mixer.py**: `fetch_music_clip()` checks S3 manifest first,
  falls back to Pixabay

### Task 4.5 — Cleanup
- Replaced all `print()` statements in handler files with `logger` / `log.warning()`
- `__main__` block print calls preserved (intentional local-testing output)

---

## Known Remaining Issues (NEEDS_REVIEW)

| Issue | Severity | Notes |
|---|---|---|
| `nexus-intro-outro` not yet wired into ASL | Low | The ASL state machine cannot be modified per AGENTS.md. Integration is done at EDL level in nexus-editor. |
| `nexus-upload` SEO only runs when `script_text` is in the SQS message body | Low | Upstream callers (nexus-notify or manual approval) must pass `script_text` in the upload job metadata |
| `true_crime` Polly voice uses `Gregory` (neural only) | Low | Tier 3 fallback uses `Matthew` (standard voice) — already handled in `POLLY_STANDARD_VOICE_MAP` |
| S3 music library requires manual one-time setup via `setup_music_library.py` | Medium | Until the library is populated, runtime falls back to Pixabay transparently |
| `nexus-shorts/broll_fetcher.py` `fetch_broll_clip()` callers may not pass `profile=` | Low | Existing callers still work (default `profile=None`) |

---

## Estimated Cost Per Video (Before vs After)

| Component | Before Phase 3/4 | After Phase 3/4 |
|---|---|---|
| Nova Reel (Shorts) | ~$0.40 per short (6 clips) | $0.00 for true_crime (Pexels-first) |
| Pexels API | Free tier | Free tier (no change) |
| ElevenLabs TTS | ~$0.003/char | Unchanged (3-tier cascade) |
| Claude Sonnet (SEO) | — | ~$0.01 per run (2 invocations) |
| Claude Sonnet (Shorts) | ~$0.005/short | Unchanged |
| S3 storage (music lib) | $0 | One-time setup + ~$0.023/GB/month |
| Total estimated per video | ~$4-8 | ~$3-6 (savings from Nova Reel reduction) |

*Costs are estimates based on us-east-1 pricing as of Q1 2026.*

---

## Verification Checklist (Task 4.6)

- [x] All 4 ECS tasks intact (nexus-audio, nexus-visuals, nexus-editor, nexus-shorts)
- [x] All 5 API routes intact (/run, /resume, /status/{id}, /outputs/{id}, /health)
- [x] nexus-shorts all 14 module files present per AGENTS.md
- [x] ElevenLabs cascade preserved — Polly is fallback, not replacement
- [x] No `os.environ` secret reads in Lambda/ECS handlers
- [x] No `print()` in handler code (only in `__main__` blocks for local testing)
- [x] EFS cleanup on completion in all ECS tasks (existing logic preserved)
- [x] X-Ray tracing present in all ECS handlers (existing `patch_all()` preserved)
- [x] `python3 -m pytest scripts/tests/ -q --tb=short` — **379 passed, 2 skipped**
