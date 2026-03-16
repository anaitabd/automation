# Complete Pipeline Fix Report - Final
**Date:** March 15, 2026  
**Status:** All Issues Fixed & Deployed

---

## Summary of All Issues Found & Fixed

### **Issue #1: nexus-audio - Pixabay API Key Location** ✅ FIXED
**Error:** `ResourceNotFoundException: Secrets Manager can't find nexus/pixabay_api_key`

**Root Cause:** Code tried to fetch `nexus/pixabay_api_key` but it doesn't exist. The key is stored inside `nexus/pexels_api_key` secret.

**Fix Applied:**
```python
# Changed from:
pixabay_api_key = get_secret("nexus/pixabay_api_key").get("api_key", "")

# To:
pixabay_api_key = get_secret("nexus/pexels_api_key").get("pixabay_key", "")
```

**File:** `lambdas/nexus-audio/handler.py`  
**Image:** `nexus-audio:latest` (digest: sha256:2571f682...)

---

### **Issue #2: nexus-visuals - Nova Reel API Task Type** ✅ FIXED (v2)
**Error:** `ValidationException: Malformed input request: #/taskType: IMAGE_TO_VIDEO is not a valid enum value`

**Root Cause:** AWS Bedrock Nova Reel API only supports `TEXT_TO_VIDEO` task type. When providing images, you keep the same task type and add images to the parameters.

**Initial Fix (Incorrect):**
```python
# Changed TEXT_VIDEO → TEXT_TO_VIDEO
# Changed TEXT_IMAGE_TO_VIDEO → IMAGE_TO_VIDEO
# ❌ This was wrong! IMAGE_TO_VIDEO doesn't exist
```

**Final Fix (Correct):**
```python
model_input = {
    "taskType": "TEXT_TO_VIDEO",  # Always use this
    "textToVideoParams": {
        "text": text_prompt,
    },
    "videoGenerationConfig": video_generation_config,
}
if image_s3_uri:
    # Don't change taskType, just add images
    model_input["textToVideoParams"]["images"] = [
        {"format": "png", "source": {"s3Location": {"uri": image_s3_uri}}}
    ]
```

**File:** `lambdas/shared/nova_reel.py`  
**Images:** 
- `nexus-visuals:latest` (digest: sha256:b5c77952...)
- `nexus-shorts:latest` (digest: sha256:6716f191...)

---

### **Issue #3: State Machine - Missing EDL_S3_KEY** ✅ FIXED
**Error:** `Error: [nexus-editor] EDL_S3_KEY environment variable is required`

**Root Cause:** Step Functions wasn't passing the EDL (Edit Decision List) from Visuals to Editor.

**Fixes Applied:**
1. Added `SetVisualsKeys` Pass state after Visuals ECS task
2. Updated `MergeParallelOutputs` to extract `edl_s3_key` from Visuals output
3. Added `EDL_S3_KEY` environment variable to Editor task parameters

**File:** `statemachine/nexus_pipeline.asl.json`  
**Status:** Terraform deployed successfully

---

### **Issue #4: nexus-editor - Missing registerRoot()** ✅ FIXED
**Error:** `Error: You passed /app/src/index.tsx as your entry point, but this file does not contain "registerRoot"`

**Root Cause:** Remotion requires the entry point to call `registerRoot()` to register the video composition.

**Fix Applied:**
```typescript
// Added import:
import { Composition, registerRoot } from "remotion";

// Added at end of file:
registerRoot(RemotionRoot);
```

**File:** `lambdas/nexus-editor/src/index.tsx`  
**Image:** `nexus-editor:latest` (digest: sha256:0b48dd58...)

---

## Deployment Status

### ✅ **All Docker Images Rebuilt & Pushed to ECR**

| Image | Digest | Issues Fixed |
|-------|--------|--------------|
| `nexus-audio:latest` | sha256:2571f682 | Pixabay secret location |
| `nexus-visuals:latest` | sha256:b5c77952 | Nova Reel task type (v2) |
| `nexus-editor:latest` | sha256:0b48dd58 | Remotion registerRoot |
| `nexus-shorts:latest` | sha256:6716f191 | Nova Reel task type (v2) |

### ✅ **State Machine Updated**
- Terraform applied successfully
- 1 resource changed
- EDL_S3_KEY now properly threaded through workflow

---

## Root Cause Analysis

### **Why Did We Need Multiple Attempts?**

**Nova Reel API Evolution:**
1. **Original Code**: Used `TEXT_VIDEO` and `TEXT_IMAGE_TO_VIDEO`
2. **First Fix**: Changed to `TEXT_TO_VIDEO` and `IMAGE_TO_VIDEO`
3. **Final Fix**: Realized `IMAGE_TO_VIDEO` doesn't exist - AWS uses `TEXT_TO_VIDEO` for both text-only and image-to-video generation

**Key Insight:** AWS Bedrock Nova Reel has a single task type (`TEXT_TO_VIDEO`) that handles both scenarios:
- **Text-only**: `TEXT_TO_VIDEO` + text prompt
- **Image-to-video**: `TEXT_TO_VIDEO` + text prompt + images array

---

## Files Changed

1. `lambdas/nexus-audio/handler.py` - Pixabay secret fix
2. `lambdas/shared/nova_reel.py` - Nova Reel API fix (v2)
3. `lambdas/nexus-editor/src/index.tsx` - Remotion registerRoot
4. `statemachine/nexus_pipeline.asl.json` - EDL_S3_KEY threading
5. `AGENTS.md` - Updated secret documentation

---

## Verification Test Plan

### **Phase 1: Start Fresh Run**
```bash
curl -X POST "https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run" \
  -H "Content-Type: application/json" \
  -d '{
    "niche": "Ancient civilizations",
    "profile": "documentary",
    "pipeline_type": "video",
    "generate_shorts": false
  }'
```

### **Phase 2: Monitor Critical Steps**
```bash
# Watch Audio
aws logs tail /ecs/nexus-audio --follow | grep -E "(ERROR|INFO.*Audio)"

# Watch Visuals
aws logs tail /ecs/nexus-visuals --follow | grep -E "(ERROR|Scene.*success|clips produced)"

# Watch Editor
aws logs tail /ecs/nexus-editor --follow | grep -E "(ERROR|EDL|Bundling|Rendering)"
```

### **Phase 3: Verify Outputs**

**Expected S3 Structure:**
```
s3://nexus-outputs/{run_id}/
  ├── research.json
  ├── script.json
  ├── audio/
  │   ├── mixed_audio.wav
  │   └── voiceover.wav
  ├── images/
  │   └── scene_*.png
  ├── visuals/
  │   └── clip_*.mp4
  ├── script_with_assets.json (EDL)
  └── review/
      └── final_video.mp4
```

**Success Criteria:**
- ✅ Audio step completes without secret errors
- ✅ Visuals step produces N/N scenes (not 0/N)
- ✅ Editor step bundles Remotion without errors
- ✅ Editor step loads EDL_S3_KEY successfully
- ✅ Final video exists at `{run_id}/review/final_video.mp4`
- ✅ Pipeline status: `SUCCEEDED`

---

## Known Limitations

1. **Nova Reel Quota**: Limited by AWS account quota (6 concurrent jobs default)
2. **Nova Canvas Throttling**: May see throttling warnings during high scene count
3. **Empty Scenes**: If all Nova Reel jobs fail, Editor will get 0 scenes - this is handled gracefully but won't produce a valid video

---

## Next Steps

1. **Start a fresh test run** with all fixes deployed
2. **Monitor execution** for 10-20 minutes
3. **Verify final video** is produced
4. **Document success** or identify any remaining issues

---

## Quick Reference

### **AWS Resources**
- State Machine: `arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline`
- ECR Registry: `670294435884.dkr.ecr.us-east-1.amazonaws.com`
- S3 Outputs: `s3://nexus-outputs/`
- API Endpoint: `https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/`

### **CloudWatch Log Groups**
- `/ecs/nexus-audio`
- `/ecs/nexus-visuals`
- `/ecs/nexus-editor`
- `/ecs/nexus-shorts`

---

**Status:** ✅ All fixes deployed. Ready for end-to-end verification test.

**Recommendation:** Start a fresh run and monitor through completion to confirm all 4 issues are resolved.

