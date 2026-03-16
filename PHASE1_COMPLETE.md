# PHASE 1 COMPLETE — Pipeline End-to-End Fix

Date: 2026-03-16

## Summary

Three critical fixes applied to enable end-to-end pipeline execution.

---

## Files Changed

### Task 1.1 — Fix Visuals Bedrock Throttling

| File | Change |
|------|--------|
| `lambdas/shared/nova_canvas.py` | Added `threading`, `random` imports; added module-level `bedrock_client` and `bedrock_semaphore`; added `invoke_with_backoff(client, payload, run_id, max_retries)` helper; replaced manual retry loop in `generate_image` with `invoke_with_backoff` |
| `lambdas/shared/nova_reel.py` | Added `threading`, `random`, `logging` imports; added module-level `bedrock_client` and `bedrock_semaphore`; added `invoke_with_backoff(client, fn_name, kwargs, run_id, max_retries)` helper; wrapped `start_async_invoke` in `_start_generation` with `invoke_with_backoff`; `generate_video` now uses module-level `bedrock_client` instead of creating one per call |
| `lambdas/nexus-visuals/handler.py` | Already had `bedrock_semaphore`, `invoke_with_backoff`, scene staggering (`time.sleep(i * 0.5)`), and S3 error writing — no changes needed |

**Why:** `ThreadPoolExecutor` with multiple workers was firing simultaneous Bedrock calls, causing `ThrottlingException` and empty EDL output. The semaphore caps concurrent calls to 4; exponential backoff with jitter retries throttled calls up to 5 times.

---

### Task 1.2 — Fix Shorts Parameters Not Forwarded

| File | Change |
|------|--------|
| `lambdas/nexus-api/handler.py` | Added `pipeline_type = body.get("pipeline_type", "video")` in `_handle_run()`; added `"pipeline_type": pipeline_type` to SFN execution input; changed `shorts_tiers` default from `"micro,short,mid,full"` (string) to `[]` (list) in both `_handle_run()` and `_handle_resume()`; added `"pipeline_type"` to `_handle_resume()` SFN payload |
| `scripts/tests/test_api_handler.py` | Updated `test_defaults_optional_fields` assertion: `shorts_tiers` default is now `[]` |
| `scripts/tests/test_regression.py` | Updated `test_defaults_applied_when_fields_absent` assertion: `shorts_tiers` default is now `[]` |

**Why:** `pipeline_type` was never forwarded to Step Functions, preventing the ASL from knowing which pipeline mode to run. `shorts_tiers` default changed to empty list per spec (callers must explicitly opt into tiers).

---

### Task 1.3 — Replace Perplexity with Bedrock Web Search

| File | Change |
|------|--------|
| `lambdas/nexus-research/handler.py` | Removed `urllib.request`, `urllib.error` imports; removed `_http_post()` and `_perplexity_search()` functions; removed `get_secret("nexus/perplexity_api_key")` call; added `threading`, `random` imports; added module-level `bedrock` client, `bedrock_semaphore`, and `invoke_with_backoff()`; added `_bedrock_web_search()` using Bedrock Claude with `web_search_20250305` native tool; updated `_bedrock_select_topic()` to use module-level `bedrock` client and `invoke_with_backoff()`; updated `lambda_handler` to call `_bedrock_web_search()` instead of `_perplexity_search()`; updated all log messages to use `logger.info(f"[{run_id}] research: ...")` format |
| `scripts/tests/test_research_handler.py` | Replaced Perplexity-specific tests with Bedrock web search tests; added `test_bedrock_web_search_calls_invoke_model`, `test_bedrock_web_search_extracts_text_blocks`, `test_bedrock_web_search_propagates_errors`, `test_no_perplexity_secret_fetched`; fixed model ID assertion in `test_research_uses_sonnet_4_5_model` to use `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| `AGENTS.md` | Updated `nexus/perplexity_api_key` note (Secrets Manager entry retained, runtime usage removed); updated "Known bug" section to "Fixed (Phase 1)" for `_handle_run` forwarding |

**Why:** Perplexity adds external dependency, cost, and rate limit risk. Claude Sonnet via Bedrock with native `web_search_20250305` tool performs equivalent YouTube trend research with no external API key required at runtime.

**Not changed:**
- `terraform/modules/secrets/` — `nexus/perplexity_api_key` secret definition kept per spec

---

## Task 1.4 — Verification

### invoke_with_backoff is the only Bedrock retry mechanism
- `lambdas/nexus-visuals/handler.py`: ✅ uses `invoke_with_backoff(bedrock.converse, payload)` with `bedrock_semaphore`
- `lambdas/shared/nova_canvas.py`: ✅ uses `invoke_with_backoff(bedrock_client, payload)` with `bedrock_semaphore`
- `lambdas/shared/nova_reel.py`: ✅ uses `invoke_with_backoff(client, "start_async_invoke", kwargs)` with `bedrock_semaphore`
- `lambdas/nexus-research/handler.py`: ✅ uses `invoke_with_backoff(bedrock.invoke_model, payload, run_id)` with `bedrock_semaphore`

### No print() statements introduced
- Only pre-existing `print()` at `nexus-visuals/handler.py:351` inside `if __name__ == "__main__":` (CLI debug only)

### run_id, profile, dry_run preserved in all handler outputs
- `nexus-research/handler.py`: returns `{"run_id", "profile", "dry_run", ...}` ✅
- `nexus-visuals/handler.py`: returns `{"run_id", "profile", "dry_run", ...}` ✅
- `nexus-api/handler.py`: forwards all fields to SFN ✅

### Test results
```
318 passed, 1 failed (pre-existing: test_non_auth_error_does_not_fallback — NoRegionError, unrelated to Phase 1), 2 skipped
```
