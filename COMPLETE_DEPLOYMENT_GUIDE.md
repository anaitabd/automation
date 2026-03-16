# COMPLETE DEPLOYMENT GUIDE - All Fixes

## Current Status Summary

### ✅ Code Fixes Completed (6 Total)

| # | Issue | Component | Status | Deployed? |
|---|-------|-----------|--------|-----------|
| 1 | Pixabay secret location | nexus-audio | ✅ Fixed | ✅ Yes |
| 2 | Nova Reel API task type | nexus-visuals | ⚠️ Partial | ✅ Yes |
| 3 | Missing EDL_S3_KEY | State Machine | ✅ Fixed | ✅ Yes |
| 4 | Remotion registerRoot | nexus-editor | ✅ Fixed | ❌ No |
| 5 | Remotion file:// path | nexus-editor | ✅ Fixed | ❌ No |
| 6 | Bedrock throttling | nexus-script | ✅ Fixed | ❌ **NEW** |

---

## Immediate Actions Required

### 🔴 CRITICAL: Deploy Script Lambda (Fix #6)

**Why:** Current run failed at Script step due to throttling with no retries.

**Quick Deploy (5 minutes):**
```bash
cd /Users/abdallahnait/Documents/GitHub/automation/lambdas/nexus-script

# Create zip
zip -q -r /tmp/script-fix.zip handler.py nexus_pipeline_utils.py

# Deploy
aws lambda update-function-code \
  --function-name nexus-script \
  --zip-file fileb:///tmp/script-fix.zip \
  --region us-east-1

# Verify
aws lambda get-function \
  --function-name nexus-script \
  --query 'Configuration.[LastModified,CodeSize]' \
  --output table

# Cleanup
rm /tmp/script-fix.zip
```

---

### 🟡 IMPORTANT: Deploy Editor Image (Fixes #4 & #5)

**Why:** Editor still has registerRoot error and file:// path issue.

**Deploy:**
```bash
cd /Users/abdallahnait/Documents/GitHub/automation

# Login to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 670294435884.dkr.ecr.us-east-1.amazonaws.com

# Build with versioned tag
docker build --platform linux/arm64 \
  -f lambdas/nexus-editor/Dockerfile \
  -t 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:v3-complete \
  -t 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:latest \
  .

# Push both tags
docker push 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:v3-complete
docker push 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:latest

# Force ECS task definition update
cd terraform
terraform taint module.compute.aws_ecs_task_definition.editor
terraform apply -target=module.compute.aws_ecs_task_definition.editor -auto-approve
```

---

### 🟠 INVESTIGATE: Nova Reel Still Failing

**Evidence from last run:**
- 12 images created ✅
- 0 video clips created ❌
- 12 manifest.json files (53 bytes - likely errors)

**Check manifest:**
```bash
aws s3 cp s3://nexus-outputs/eb6d0db6-7f54-411b-8c4b-1e093396aed7/clips/scene_001/jv9lvpsvjcb3/manifest.json -
```

**If manifest shows errors, might need another fix for nova_reel.py**

---

## Complete Deployment Script

Save this as `deploy_all_fixes.sh`:

```bash
#!/bin/bash
set -e

cd /Users/abdallahnait/Documents/GitHub/automation

echo "======================================"
echo "DEPLOYING ALL PENDING FIXES"
echo "======================================"
echo ""

# FIX #6: Script Lambda (Bedrock throttling)
echo "1️⃣  Deploying Script Lambda (Fix #6)..."
cd lambdas/nexus-script
zip -q -r /tmp/script-fix.zip handler.py nexus_pipeline_utils.py
aws lambda update-function-code \
  --function-name nexus-script \
  --zip-file fileb:///tmp/script-fix.zip \
  --region us-east-1 > /dev/null
rm /tmp/script-fix.zip
echo "   ✅ Script Lambda deployed"

# FIX #4 & #5: Editor Image
cd /Users/abdallahnait/Documents/GitHub/automation
echo ""
echo "2️⃣  Building Editor image (Fixes #4 & #5)..."
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 670294435884.dkr.ecr.us-east-1.amazonaws.com 2>/dev/null

docker build --platform linux/arm64 \
  -f lambdas/nexus-editor/Dockerfile \
  -t 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:v3-complete \
  -t 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:latest \
  . > /dev/null 2>&1

echo "   ✅ Editor image built"

echo ""
echo "3️⃣  Pushing Editor image to ECR..."
docker push 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:v3-complete > /dev/null 2>&1
docker push 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:latest > /dev/null 2>&1
echo "   ✅ Editor image pushed"

echo ""
echo "4️⃣  Updating ECS task definition..."
cd terraform
terraform taint module.compute.aws_ecs_task_definition.editor > /dev/null 2>&1 || true
terraform apply -target=module.compute.aws_ecs_task_definition.editor -auto-approve > /dev/null 2>&1
echo "   ✅ ECS task definition updated"

echo ""
echo "======================================"
echo "✅ ALL FIXES DEPLOYED"
echo "======================================"
echo ""
echo "Next steps:"
echo "1. Wait 30 seconds for Lambda to be ready"
echo "2. Run a new pipeline test"
echo "3. Monitor for Bedrock throttling (should retry now)"
echo "4. Check if Nova Reel generates videos"
echo ""
```

**Run it:**
```bash
chmod +x deploy_all_fixes.sh
./deploy_all_fixes.sh
```

---

## After Deployment: Run Test

### Option A: Fresh Run
```bash
curl -X POST "https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run" \
  -H "Content-Type: application/json" \
  -d '{
    "niche": "Ancient civilizations. Lost histories. Forgotten worlds.",
    "profile": "documentary",
    "pipeline_type": "video",
    "generate_shorts": false
  }'
```

### Option B: Resume Failed Run (if Script data exists)
```bash
curl -X POST "https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/resume" \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "cbc8a51a-67ae-4f4f-ade2-53bd803f71ef",
    "resume_from": "Script"
  }'
```

---

## What to Watch For

### ✅ Success Indicators:
1. **Script**: Should see retry logs if throttled, but complete successfully
2. **Audio**: Should complete without secret errors
3. **Visuals**: **KEY TEST** - Should produce 12 .mp4 files (not just manifests)
4. **Editor**: Should bundle without registerRoot error
5. **Editor**: Should render without file:// download error
6. **Final**: Video at `s3://.../review/final_video.mp4`

### ❌ Failure Points to Check:
1. **Script still throttles** → Check Lambda logs for retry attempts
2. **Nova Reel still fails** → Check manifest content for API error
3. **Editor crashes** → Check if new image was actually pulled
4. **Empty video** → Means Visuals produced 0 clips

---

## Verification Commands

```bash
# 1. Check Lambda was updated
aws lambda get-function --function-name nexus-script \
  --query 'Configuration.LastModified'

# 2. Check Editor image digest
aws ecs describe-task-definition \
  --task-definition nexus-editor \
  --query 'taskDefinition.containerDefinitions[0].image'

# 3. Monitor execution
RUN_ID="<your-run-id>"
watch -n 5 "aws stepfunctions describe-execution \
  --execution-arn arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:$RUN_ID \
  --query 'status'"

# 4. Check for video clips
aws s3 ls s3://nexus-outputs/$RUN_ID/visuals/ --recursive

# 5. Check Script logs for retries
aws logs tail /aws/lambda/nexus-script --since 10m | grep -i "retry\|throttl"
```

---

## Summary of Fixes

### Fix #1: Pixabay Secret ✅ DEPLOYED
- File: `lambdas/nexus-audio/handler.py`
- Change: Read from `nexus/pexels_api_key` with key `pixabay_key`

### Fix #2: Nova Reel API ⚠️ PARTIAL
- File: `lambdas/shared/nova_reel.py`
- Change: Keep `TEXT_TO_VIDEO` taskType always
- Status: Deployed but still producing 0 videos - needs investigation

### Fix #3: EDL_S3_KEY ✅ DEPLOYED
- File: `statemachine/nexus_pipeline.asl.json`
- Changes: Added SetVisualsKeys, updated MergeParallelOutputs, added env var

### Fix #4: Remotion registerRoot ⏳ PENDING DEPLOYMENT
- File: `lambdas/nexus-editor/src/index.tsx`
- Change: Added `registerRoot(RemotionRoot)` call

### Fix #5: Remotion file:// Path ⏳ PENDING DEPLOYMENT
- File: `lambdas/nexus-editor/src/DocumentaryComposition.tsx`
- Change: Removed `file://` protocol prefix

### Fix #6: Bedrock Throttling ⏳ PENDING DEPLOYMENT (NEW)
- File: `lambdas/nexus-script/handler.py`
- Changes:
  - boto3 Config: `retries={"max_attempts": 3, "mode": "adaptive"}`
  - Enhanced retry logic with exponential backoff for throttling

---

## Timeline Estimate

1. Deploy Script Lambda: **2 minutes**
2. Build & push Editor image: **3-5 minutes**
3. Update ECS task definition: **1 minute**
4. Lambda cold start delay: **30 seconds**
5. Run new test pipeline: **15-20 minutes**

**Total: ~25-30 minutes** from start to verified result

---

**Current Status:** 3/6 fixes deployed, 3/6 pending  
**Blockers:** Script throttling (prevents any run from completing)  
**Priority:** Deploy Fix #6 immediately, then Fixes #4 & #5, then investigate Nova Reel


