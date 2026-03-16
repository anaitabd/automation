# Editor Task Failure Fix Report
**Date:** March 15, 2026  
**Run ID:** `f6a4812c-7f38-4215-8988-de0589dea73c`  
**Status:** Fixed and Deployed

---

## Issue Identified

### **nexus-editor ECS Task Failure**
**Error:** `Error: [nexus-editor] EDL_S3_KEY environment variable is required`

**Root Cause:**
The Step Functions state machine was not properly passing the `edl_s3_key` (Edit Decision List with visual assets) from the Visuals step to the Editor task. The Editor task requires this file to know which video clips to assemble.

**Missing Data Flow:**
```
Visuals → outputs edl_s3_key
         ↓ (MISSING)
MergeParallelOutputs → didn't extract edl_s3_key
         ↓ (MISSING)
Editor → couldn't receive EDL_S3_KEY env var → CRASHED
```

---

## Fixes Applied

### 1. Added `SetVisualsKeys` Pass State
**File:** `statemachine/nexus_pipeline.asl.json`

The Visuals branch now has a Pass state (similar to Audio's `SetAudioKeys`) that properly formats and outputs the EDL key:

```json
"SetVisualsKeys": {
  "Type": "Pass",
  "Parameters": {
    "run_id.$": "$.run_id",
    "profile.$": "$.profile",
    "dry_run.$": "$.dry_run",
    "niche.$": "$.niche",
    "subnets.$": "$.subnets",
    "generate_shorts.$": "$.generate_shorts",
    "shorts_tiers.$": "$.shorts_tiers",
    "channel_id.$": "$.channel_id",
    "script_s3_key.$": "$.script_s3_key",
    "title.$": "$.title",
    "total_duration_estimate.$": "$.total_duration_estimate",
    "edl_s3_key.$": "States.Format('{}/script_with_assets.json', $.run_id)"
  },
  "ResultPath": "$",
  "End": true
}
```

### 2. Updated `MergeParallelOutputs` State
**File:** `statemachine/nexus_pipeline.asl.json`

Added `edl_s3_key` extraction from the Visuals output (parallel branch $[1]):

```json
"MergeParallelOutputs": {
  "Type": "Pass",
  "Parameters": {
    "run_id.$": "$[0].run_id",
    "profile.$": "$[0].profile",
    // ... other fields ...
    "mixed_audio_s3_key.$": "$[0].mixed_audio_s3_key",
    "edl_s3_key.$": "$[1].edl_s3_key",  // ← ADDED
    "subnets.$": "$[0].subnets",
    // ... rest ...
  }
}
```

### 3. Added `EDL_S3_KEY` to Editor Task Environment
**File:** `statemachine/nexus_pipeline.asl.json`

The Editor ECS task now receives the EDL key as an environment variable:

```json
"Environment": [
  {"Name": "RUN_ID", "Value.$": "$.run_id"},
  {"Name": "PROFILE", "Value.$": "$.profile"},
  {"Name": "SCRIPT_S3_KEY", "Value.$": "$.script_s3_key"},
  {"Name": "EDL_S3_KEY", "Value.$": "$.edl_s3_key"},  // ← ADDED
  {"Name": "MIXED_AUDIO_S3_KEY", "Value.$": "$.mixed_audio_s3_key"},
  {"Name": "TITLE", "Value.$": "$.title"}
]
```

---

## Deployment Status

✅ **State Machine Updated**
- Terraform applied successfully
- Resource changes: 0 added, 1 changed, 0 destroyed
- State Machine ARN: `arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline`

---

## How to Resume the Failed Run

Since the failed run completed Research, Script, Audio, and Visuals successfully, you can resume from the Editor step:

### Option 1: Resume via API
```bash
curl -X POST "https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/resume" \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "f6a4812c-7f38-4215-8988-de0589dea73c",
    "resume_from": "ContentAssembly"
  }'
```

### Option 2: Resume via Python Script
```bash
cd /Users/abdallahnait/Documents/GitHub/automation
python3 scripts/resume_run.py \
  --run-id f6a4812c-7f38-4215-8988-de0589dea73c \
  --resume-from ContentAssembly
```

### Option 3: Start Fresh Run
```bash
# Via dashboard: http://localhost:3000
# Or via API:
curl -X POST "https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run" \
  -H "Content-Type: application/json" \
  -d '{
    "niche": "your niche here",
    "profile": "documentary",
    "pipeline_type": "video",
    "generate_shorts": false
  }'
```

---

## Data Flow After Fix

```
Research → Script
            ↓
AudioVisuals Parallel:
  ├── Audio → SetAudioKeys → outputs mixed_audio_s3_key
  └── Visuals → SetVisualsKeys → outputs edl_s3_key
            ↓
MergeParallelOutputs → merges both outputs
            ↓
ContentAssembly Parallel:
  ├── Editor (receives EDL_S3_KEY ✅) → final_video.mp4
  └── Shorts (optional)
            ↓
Thumbnail → Notify
```

---

## Files Changed

1. `/Users/abdallahnait/Documents/GitHub/automation/statemachine/nexus_pipeline.asl.json`
   - Added `SetVisualsKeys` Pass state
   - Updated `MergeParallelOutputs` to include `edl_s3_key`
   - Added `EDL_S3_KEY` env var to Editor task

---

## Verification Steps

1. **Check State Machine** ✅
   ```bash
   aws stepfunctions describe-state-machine \
     --state-machine-arn arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline \
     --query 'definition' --output text | jq '.States.ContentAssembly.Branches[0].States.Editor.Parameters.Overrides.ContainerOverrides[0].Environment[] | select(.Name == "EDL_S3_KEY")'
   ```

2. **Monitor Editor Logs**
   ```bash
   aws logs tail /ecs/nexus-editor --follow
   ```

3. **Verify EDL File Exists** (before resuming)
   ```bash
   aws s3 ls s3://nexus-outputs/f6a4812c-7f38-4215-8988-de0589dea73c/script_with_assets.json
   ```

---

## Related Previous Fixes

This is the **third fix** in the deployment series:

1. **Fix #1** - nexus-audio: Pixabay API key secret location
2. **Fix #2** - nexus-visuals: Nova Reel task type enums
3. **Fix #3** - State Machine: EDL_S3_KEY missing from Editor (this fix)

All previous Docker image fixes remain active:
- `nexus-audio:latest` (digest: sha256:2571f682...)
- `nexus-visuals:latest` (digest: sha256:53547079...)
- `nexus-shorts:latest` (digest: sha256:b2599d3e...)

---

## Next Steps

1. **Resume the run** using one of the methods above
2. **Monitor execution** via dashboard or logs
3. **Verify Editor completion** - look for output at:
   ```
   s3://nexus-outputs/f6a4812c-7f38-4215-8988-de0589dea73c/review/final_video.mp4
   ```
4. Pipeline should complete: Editor → Thumbnail → Notify

---

**Status:** ✅ Fix deployed. State machine updated. Ready to resume.

**Important Note:** No ECS container rebuild needed for this fix. Only the Step Functions orchestration was updated.

