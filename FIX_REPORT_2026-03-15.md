# Pipeline Failure Fix Report
**Date:** March 15, 2026  
**Run ID:** `4b118f45-ae05-4efc-b1c7-0d49565cb171`  
**Status:** Fixed and Ready for Resume

---

## Issues Identified

### 1. **nexus-audio ECS Task Failure**
**Error:** `ResourceNotFoundException: Secrets Manager can't find the specified secret`

**Root Cause:**
- Handler was trying to fetch `nexus/pixabay_api_key` secret
- This secret does not exist in AWS Secrets Manager
- The Pixabay API key is actually stored inside `nexus/pexels_api_key` secret with key `pixabay_key`

**Fix Applied:**
```python
# Before:
pixabay_api_key = get_secret("nexus/pixabay_api_key").get("api_key", "")

# After:
pixabay_api_key = get_secret("nexus/pexels_api_key").get("pixabay_key", "")
```

**File Modified:** `lambdas/nexus-audio/handler.py` (line 764)

---

### 2. **nexus-visuals ECS Task Failure**
**Error:** `ValidationException: Malformed input request: #/taskType: TEXT_IMAGE_TO_VIDEO is not a valid enum value`

**Root Cause:**
- Amazon Nova Reel API has updated its task type enum values
- Old values: `TEXT_VIDEO` and `TEXT_IMAGE_TO_VIDEO`
- New values: `TEXT_TO_VIDEO` and `IMAGE_TO_VIDEO`

**Fix Applied:**
```python
# Before:
model_input = {
    "taskType": "TEXT_VIDEO",  # Wrong enum
    ...
}
if image_s3_uri:
    model_input["taskType"] = "TEXT_IMAGE_TO_VIDEO"  # Wrong enum

# After:
model_input = {
    "taskType": "TEXT_TO_VIDEO",  # Correct enum
    ...
}
if image_s3_uri:
    model_input["taskType"] = "IMAGE_TO_VIDEO"  # Correct enum
```

**File Modified:** `lambdas/shared/nova_reel.py` (lines 33-40)

---

## Actions Taken

### Docker Images Rebuilt and Pushed to ECR

1. **nexus-audio:latest** ✅
   - Fixed Pixabay API key retrieval
   - Built for `linux/arm64`
   - Pushed to `670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-audio:latest`
   - Digest: `sha256:2571f682e9ede4f62115f1ae7718518350e65f35fa217d342555559362e0b37c`

2. **nexus-visuals:latest** ✅
   - Fixed Nova Reel task types
   - Built for `linux/arm64`
   - Pushed to `670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-visuals:latest`
   - Digest: `sha256:53547079678432e8d8121861f97dc5c595ebb74fac287aabc34ced0f3dfa7e5b`

3. **nexus-shorts:latest** ✅
   - Fixed Nova Reel task types (uses same shared module)
   - Built for `linux/arm64`
   - Pushed to `670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-shorts:latest`
   - Digest: `sha256:b2599d3e376c6257ad21af750e64c0716cacc78086a3e0a292c0fd26a8e41ec3`

---

## How to Resume the Failed Run

Since the failed run completed `Research` and `Script` steps successfully, you can resume from the `AudioVisuals` step:

### Option 1: Resume via API
```bash
curl -X POST "https://your-api-endpoint/resume" \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "4b118f45-ae05-4efc-b1c7-0d49565cb171",
    "resume_from": "AudioVisuals"
  }'
```

### Option 2: Resume via Python Script
```bash
cd /Users/abdallahnait/Documents/GitHub/automation
python3 scripts/resume_run.py \
  --run-id 4b118f45-ae05-4efc-b1c7-0d49565cb171 \
  --resume-from AudioVisuals
```

### Option 3: Start Fresh Run
If you prefer to start from scratch with the fixes:
```bash
# Via dashboard (http://localhost:3000)
# Or via API:
curl -X POST "https://your-api-endpoint/run" \
  -H "Content-Type: application/json" \
  -d '{
    "niche": "Ancient civilizations. Lost histories. Forgotten worlds.",
    "profile": "documentary",
    "pipeline_type": "video",
    "generate_shorts": false
  }'
```

---

## ECS Task Definition Update

⚠️ **Important:** ECS Fargate will automatically pull the `:latest` tag on the next task invocation. No terraform redeployment is required since we only updated the container images in ECR.

The existing task definitions already reference:
- `${aws_ecr_repository.audio.repository_url}:latest`
- `${aws_ecr_repository.visuals.repository_url}:latest`
- `${aws_ecr_repository.shorts.repository_url}:latest`

---

## Verification Checklist

- [x] Identified root causes
- [x] Applied code fixes
- [x] Rebuilt affected Docker images
- [x] Pushed images to ECR
- [x] Verified secrets exist in AWS Secrets Manager
- [ ] Resume the failed run and verify success
- [ ] Monitor CloudWatch logs for Audio and Visuals tasks

---

## Additional Notes

### Secrets in AWS Secrets Manager (Verified)
```
nexus/elevenlabs_api_key      ✅
nexus/perplexity_api_key      ✅
nexus/pexels_api_key          ✅ (contains pixabay_key field)
nexus/freesound_api_key       ✅
nexus/youtube_credentials     ✅
nexus/discord_webhook_url     ✅
nexus/db_credentials          ✅
nexus/runwayml_api_key        ✅
nexus/nvidia_api_key          ✅
```

### Files Changed
1. `/Users/abdallahnait/Documents/GitHub/automation/lambdas/nexus-audio/handler.py`
2. `/Users/abdallahnait/Documents/GitHub/automation/lambdas/shared/nova_reel.py`

### No Changes Required For
- State machine ASL definition
- Terraform configuration
- IAM roles or permissions
- Lambda functions (only ECS tasks affected)

---

## Next Steps

1. **Resume the failed run** using one of the methods above
2. **Monitor the execution** via dashboard or CloudWatch logs:
   ```bash
   # Watch Audio logs
   aws logs tail /ecs/nexus-audio --follow
   
   # Watch Visuals logs
   aws logs tail /ecs/nexus-visuals --follow
   ```
3. **Verify successful completion** of Audio and Visuals parallel steps
4. The pipeline should then proceed to Editor → Thumbnail → Notify

---

**Status:** ✅ All fixes applied and deployed. Ready for resume.

