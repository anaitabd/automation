# Testing the Script Lambda Fix

## Deployment Verification

The Script Lambda fix has been successfully deployed to AWS:

```bash
✅ Lambda Function: nexus-script
✅ Region: us-east-1
✅ Account: 670294435884
✅ Status: Successful
```

## Test the Fix with the Failed Run

You can now resume the failed run to test if the fix works:

### Option 1: Resume from Script Step (Recommended)

This will re-run the Script step with the new fix:

```bash
python scripts/resume_run.py 9450f8e9-62d9-4123-8a15-85721b391667 --from Script
```

### Option 2: Dry Run Test (Safe)

Test without actually executing:

```bash
python scripts/resume_run.py 9450f8e9-62d9-4123-8a15-85721b391667 --from Script --dry-run
```

### What to Monitor

Once you resume the run, monitor the CloudWatch logs for:

1. **Success Indicators:**
   - `[INFO] _pass1_structure: auto-filled missing fields, continuing` — Auto-fill worked
   - `Pass 1/7: Generating script structure for` — Normal execution
   - `Script complete — factual_confidence=` — Completed successfully

2. **Validation Logs:**
   - `[WARN] _pass1_structure EDL validation failed (attempt X/3):` — Retry happening
   - Look for the list of missing fields

3. **Token Budget:**
   - `[WARN] _bedrock_call: output truncated (stop_reason=max_tokens` — Still hitting limit (shouldn't happen with 8000)

### Check Logs in Real-Time

```bash
# Watch CloudWatch logs
aws logs tail /aws/lambda/nexus-script --follow --region us-east-1

# Or check the specific log group after resuming
aws logs filter-log-events \
  --log-group-name /aws/lambda/nexus-script \
  --filter-pattern "9450f8e9-62d9-4123-8a15-85721b391667" \
  --region us-east-1 \
  --output text
```

### Expected Outcome

With the fix deployed:

- ✅ The Script step should complete successfully
- ✅ All scenes should have required fields (nova_canvas_prompt, nova_reel_prompt, etc.)
- ✅ The pipeline should proceed to Audio/Visuals parallel steps
- ✅ EDL schema validation should pass

### If Issues Persist

If the script still fails:

1. **Check the error in S3:**
   ```bash
   aws s3 cp s3://nexus-outputs/9450f8e9-62d9-4123-8a15-85721b391667/errors/Script.json -
   ```

2. **Verify the Lambda code was updated:**
   ```bash
   aws lambda get-function --function-name nexus-script --region us-east-1 | jq '.Configuration.LastModified'
   ```

3. **Check for new validation errors:**
   - The auto-fill logic may have filled fields but Claude might still be generating incomplete scenes
   - Consider reducing scene count in the prompt if this persists

## Changes Summary

The fix includes:

1. **8000 token budget** (was 6000) — 33% more capacity
2. **Validation feedback loop** — Claude learns from previous errors
3. **Auto-fill fallback** — Salvages incomplete scenes instead of failing

This 3-tier recovery strategy should handle truncation edge cases gracefully.

---

**Ready to test:** Run the resume command above to verify the fix works!

