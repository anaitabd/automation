# Cleanup Report — 2026-03-16

## Part 1 — Fix: Visuals Bedrock Throttling (`lambdas/nexus-visuals/handler.py`)

### Changes Made

| Change | Description |
|--------|-------------|
| Added `import threading`, `import random` | Required for semaphore and jitter |
| Added `from botocore.exceptions import ClientError` | For catching ThrottlingException by error code |
| Added `bedrock_semaphore = threading.Semaphore(4)` | Module-level semaphore caps concurrent Bedrock calls to 4 |
| Added `invoke_with_backoff(client, payload, max_retries=5)` | Reusable helper with exponential backoff + jitter on ThrottlingException |
| Wrapped `bedrock.converse()` in `_claude_vision_score` | Uses semaphore + 5-attempt backoff loop |
| Wrapped `nova_canvas.generate_and_upload_image()` in `_process_scene` | Acquires semaphore before each Nova Canvas call |
| Wrapped `nova_reel.generate_and_upload_video()` in `_process_scene` | Acquires semaphore before each Nova Reel call |
| Added `time.sleep(i * 0.5)` stagger in executor submission | Prevents all scenes from hitting Bedrock simultaneously |

### Root Cause
With `VISUALS_PARALLELISM=2` (default) and up to 10 scenes, up to 20 simultaneous Bedrock
calls (Nova Canvas + Nova Reel per scene) were being made. AWS quota limits caused
`ThrottlingException` on all calls, resulting in an empty EDL that crashed the Editor step.

---

## Part 2A — Dead Code / Print Statement Cleanup

### `lambdas/shared/nova_canvas.py`
- Replaced `print(f"[WARN] ...")` in retry loop with `log.warning(...)` using proper `logging` module
- Added `import logging`, `import random`, `from botocore.exceptions import ClientError`
- Updated retry logic to use exponential backoff with jitter on `ThrottlingException` (separate from generic errors)

### `lambdas/nexus-script/handler.py`
All 15 `print()` statements replaced with `log.info()` / `log.warning()` calls using the existing `log = get_logger("nexus-script")` logger:

| Location | Change |
|----------|--------|
| `_extract_json` (5 prints) | Converted to `log.info` / `log.warning` |
| `_bedrock_call` (2 prints) | Converted to `log.warning` |
| `_pass1_structure` (3 prints) | Converted to `log.warning` / `log.info` |
| `_pass_fact_integrity` (1 print) | Converted to `log.warning` |
| `_pass3_visual_cues` (1 print) | Converted to `log.warning` |
| `_pass4_pacing` (1 print) | Converted to `log.warning` |
| `_pass6_final_polish` (1 print) | Converted to `log.warning` |

---

## Part 2D — Unused Environment Variables

### `env.exemple`
- Removed `NVIDIA_API_KEY=nvapi-...` — NVIDIA NIM endpoints were removed from this project
  per AGENTS.md ("Do not introduce openai, nvidia, or nim client libraries"). No Lambda or
  ECS handler reads this variable at runtime.

---

## Part 2G — Diagnostic and Temporary Files Deleted

### Diagnostic Markdown Files (28 files)
These were analysis/fix-tracking documents generated during a debugging session
on 2026-03-15. They are not part of the permanent project documentation.

| File | Reason for Deletion |
|------|-------------------|
| `ALL_ISSUES_LIST.md` | Diagnostic findings catalog — not permanent docs |
| `COMPLETE_DEPLOYMENT_GUIDE.md` | Superseded by README.md |
| `COMPLETE_FIX_SUMMARY.md` | One-time fix summary |
| `CRITICAL_ISSUES_FOUND.md` | Issues now resolved |
| `DEPLOYMENT_COMPLETE_v6.md` | Deployment status snapshot |
| `DEPLOYMENT_VERIFICATION_STATUS.md` | Verification snapshot |
| `DIAGNOSTIC_FINDINGS.md` | Diagnostic output |
| `FINAL_DEPLOYMENT_STATUS.md` | Deployment status snapshot |
| `FINAL_FIX_DEPLOYMENT.md` | Fix deployment notes |
| `FINAL_IMPLEMENTATION_REPORT.md` | Implementation report |
| `FINAL_SOLUTION.md` | Solution notes |
| `FINAL_STATUS_REPORT.md` | Status snapshot |
| `FIX_6_BEDROCK_THROTTLING.md` | Issue now addressed in code (Part 1 above) |
| `FIX_REPORT_2026-03-15.md` | Dated fix report |
| `FIX_REPORT_COMPLETE_2026-03-15.md` | Dated fix report |
| `FIX_REPORT_EDITOR_2026-03-15.md` | Dated fix report |
| `FIX_SUMMARY.md` | Fix summary |
| `IMPLEMENTATION_COMPLETE.md` | Implementation status |
| `IMPLEMENTATION_STATUS.md` | Status snapshot |
| `IMPLEMENTATION_SUMMARY_v6.md` | Versioned summary |
| `ISSUE_CHECKLIST.md` | Issues now resolved |
| `MASTER_INDEX.md` | Index of diagnostic docs |
| `POST_MORTEM_ANALYSIS.md` | Post-mortem analysis |
| `QUICKSTART_v6.md` | Versioned quickstart — superseded by README.md |
| `QUOTA_LIMITED_SOLUTION.md` | Solution notes |
| `TESTING_GUIDE.md` | Testing notes |
| `VISUAL_SUMMARY.md` | Visual summary of issues |
| `dockeruse.md` | Docker usage notes — covered in README.md |

### Diagnostic Scripts (5 files)

| File | Reason for Deletion |
|------|-------------------|
| `diagnose_all_issues.sh` | Diagnostic shell script — not part of production pipeline |
| `diagnose_failure.py` | Diagnostic Python script with 32 `print()` statements |
| `monitor_test_run.sh` | Monitoring shell script for test runs |
| `monitor_verification.py` | Verification script with 36 `print()` statements |
| `test_deployment_v6.sh` | Versioned deployment test script |

### Other Temporary Files (1 file)

| File | Reason for Deletion |
|------|-------------------|
| `prompt.md` | AI prompt template file — not referenced by any pipeline, CI config, or script |

---

## Verification: Project Integrity After Cleanup

### Lambda Handlers — All Intact ✅
- `lambdas/nexus-api/handler.py` ✓
- `lambdas/nexus-script/handler.py` ✓
- `lambdas/nexus-visuals/handler.py` ✓ (updated — Part 1)
- `lambdas/nexus-audio/handler.py` ✓
- `lambdas/nexus-editor/handler.py` ✓
- `lambdas/nexus-notify/handler.py` ✓
- `lambdas/nexus-shorts/handler.py` ✓
- `lambdas/nexus-thumbnail/handler.py` ✓
- `lambdas/nexus-research/handler.py` ✓
- `lambdas/nexus-upload/handler.py` ✓
- `lambdas/nexus-brand-designer/handler.py` ✓
- `lambdas/nexus-channel-setup/handler.py` ✓
- `lambdas/nexus-logo-gen/handler.py` ✓

### Step Functions Definition — Intact ✅
- `statemachine/nexus_pipeline.asl.json` ✓

### Docker / ECS Configurations — Intact ✅
- `lambdas/nexus-audio/Dockerfile` ✓
- `lambdas/nexus-visuals/Dockerfile` ✓
- `lambdas/nexus-editor/Dockerfile` ✓
- `lambdas/nexus-shorts/Dockerfile` ✓
- `lambdas/nexus-intro-outro/Dockerfile` ✓

### `requirements.txt` Files — Unchanged ✅
No Python packages were added or removed. All dependencies are still used.

---

## Needs Review

The following items were flagged but left in place for further review:

| Item | Reason |
|------|--------|
| `scripts/verify_fixes.py` | References fixes from the debugging session. May be safe to remove once team has verified production stability. |
| `scripts/setup_aws.py` — `NVIDIA_API_KEY` block | Lines 289–303 still read `NVIDIA_API_KEY` from env and upsert `nexus/nvidia_api_key` secret. Harmless if env var is not set, but could be removed once the team confirms NVIDIA is fully decommissioned. |
| `deploy.sh` | Root-level deploy script — unclear if superseded by `terraform/scripts/deploy_tf.sh`. Left in place. |
