# 🔍 DIAGNOSTIC RESULTS - ALL ISSUES IDENTIFIED

**Date:** March 16, 2026 21:50 UTC  
**Run ID Analyzed:** `1a39b10f-e8db-44c2-a0bc-563f06914755`  
**Status:** ❌ FAILED (as expected)

---

## ✅ **WHAT'S WORKING** (Confirmed)

### 1. Editor EDL Validation ✅
**Our v6 fix is working perfectly!**

**Evidence from CloudWatch:**
```
[nexus-editor] Loading EDL from s3://nexus-outputs/.../script_with_assets.json
[nexus-editor] EDL loaded — 0 scenes
[nexus-editor] FATAL: EDL contains 0 scenes. Cannot render video.
[nexus-editor] FATAL: Error: Empty EDL: 0 scenes available for rendering
```

- ✅ Validation check executed
- ✅ Clear error message logged
- ✅ Failed fast with Exit Code 1
- ✅ Prevented FFmpeg crash

**Result:** Issue #3 **CONFIRMED FIXED** 🎉

---

### 2. Script Rate Limiting ✅
**No throttling errors in Script Lambda**

**Evidence:**
- Diagnostic showed **NO** Script errors in CloudWatch
- Pipeline reached Visuals step (Script completed successfully)
- Duration: Research + Script ~10-11 min (as expected with delays)

**Result:** Issue #1 **CONFIRMED FIXED** 🎉

---

### 3. Infrastructure ✅
**All components properly configured**

**Evidence:**
```
Editor Task Definition: Revision 28, Status: ACTIVE
State Machine: References nexus-editor:28
Script Lambda: Updated 2026-03-16T01:10:34
```

**Result:** Issue #2 **CONFIRMED FIXED** 🎉

---

## ❌ **WHAT'S BROKEN** (Root Cause Found)

### Issue #4: Visuals Step Throttling ⚠️ NEW DISCOVERY!

**Bedrock is throttling BOTH Nova Canvas AND Nova Reel**

**Evidence from CloudWatch logs:**
```
final_clip_key = nova_reel.generate_and_upload_video(
  File "/var/task/nova_reel.py", line 126, in generate_and_upload_video
  File "/var/task/nova_reel.py", line 92, in generate_video
  File "/var/task/nova_reel.py", line 44, in _start_generation

[WARN] nova_canvas.generate_image attempt 1/3 failed: An error occurred (ThrottlingException) when calling the InvokeModel operation (reached max retries: 4): Too many requests, please wait before trying again.
```

**What's happening:**
1. Visuals step processes 12 scenes in parallel
2. Each scene calls:
   - `nova_canvas.generate_image()` - Image generation
   - `nova_reel.generate_and_upload_video()` - Video generation
3. Both APIs hit Bedrock simultaneously
4. Bedrock throttles with `ThrottlingException`
5. All 12 scenes fail
6. Visuals step produces 0 clips
7. EDL has `"scenes": []`

**Root Cause:**
- **Account-level Bedrock quota exceeded**
- Script uses Sonnet/Opus (text models)
- Visuals uses Nova Canvas + Nova Reel (image/video models)
- All share the same Bedrock account quota
- No rate limiting in Visuals handler

---

### Issue #5: EDL with 0 Scenes ⚠️ CONSEQUENCE OF #4

**Visuals completes "successfully" but produces empty EDL**

**Evidence:**
```bash
aws s3 cp s3://nexus-outputs/1a39b10f-e8db-44c2-a0bc-563f06914755/script_with_assets.json -
# Output: {"scenes": []}  # Empty array
```

**What's happening:**
1. Visuals handler catches all exceptions
2. Continues even when all scenes fail
3. Writes empty EDL to S3
4. Returns "success" to Step Functions
5. Editor receives empty EDL
6. Editor validates and fails (correctly)

**This is working as designed** - Editor catches the error properly!

---

## 📊 **PIPELINE FLOW ANALYSIS**

### Actual Flow (Latest Run):
```
Research ✅ (~15s)
   ↓
Script ✅ (~10min) - NO throttling (delays working!)
   ↓
Audio ✅ (~1min)
   ↓  
Visuals ⚠️ (~3min) - THROTTLED, produces 0 clips
   ↓
Editor ❌ (attempted 3x) - Validates, finds 0 scenes, fails gracefully
   ↓
NotifyError 📧
```

**Total time:** ~17 minutes  
**Failure point:** Editor (correctly validates empty EDL)  
**Root cause:** Visuals throttling

---

## 🎯 **UPDATED ISSUE LIST**

| # | Issue | Status | Evidence | Priority |
|---|-------|--------|----------|----------|
| 1 | Script Bedrock Throttling | ✅ FIXED | No Script errors | - |
| 2 | Inactive Task Definition | ✅ FIXED | Rev 28 ACTIVE | - |
| 3 | Editor FFmpeg Crash | ✅ FIXED | Clear error msg | - |
| 4 | **Visuals Bedrock Throttling** | ❌ **ACTIVE** | CloudWatch logs | **HIGH** |
| 5 | EDL with 0 Scenes | ⚠️ Consequence | Empty EDL file | MEDIUM |
| 6 | Shorts Parameters | 🔍 Unconfirmed | - | MEDIUM |
| 7 | Perplexity Fact-Check | 🔍 Unconfirmed | - | LOW |

---

## 🔧 **REQUIRED FIXES**

### Fix #1: Add Rate Limiting to Visuals Handler ⚠️ **URGENT**

**Location:** `lambdas/nexus-visuals/handler.py`

**Problem:**
- Processes 12 scenes in parallel with no delays
- Each scene calls Nova Canvas + Nova Reel simultaneously
- Bedrock quota exceeded immediately

**Solution:**
```python
# In _process_one function (scene processing):

# Before nova_canvas.generate_image()
time.sleep(2)  # 2s delay before each image generation

# Before nova_reel.generate_and_upload_video()
time.sleep(3)  # 3s delay before each video generation

# OR: Reduce max_workers from 10 to 3
max_workers = min(3, len(scenes))  # Process 3 scenes at a time instead of 10
```

**Expected Impact:**
- Spreads API calls over time
- Prevents quota exhaustion
- Visuals should complete successfully

---

### Fix #2: Implement Fallback for Failed Scenes ⚠️ **IMPORTANT**

**Location:** `lambdas/nexus-visuals/handler.py`

**Problem:**
- When Nova Reel fails, scene is dropped entirely
- Results in empty EDL

**Solution:**
```python
# Fallback hierarchy:
1. Try Nova Reel (with rate limiting)
2. If fails → Try Pexels video search
3. If fails → Use Nova Canvas image with Ken Burns motion
4. If fails → Use gradient background

# Never return empty scene - always provide SOMETHING
```

---

### Fix #3: Early Failure Detection ⚠️ **MEDIUM**

**Location:** `lambdas/nexus-visuals/handler.py`

**Problem:**
- Visuals completes "successfully" with 0 clips
- Editor wastes time attempting to render

**Solution:**
```python
# After processing all scenes:
if len(processed_scenes) == 0:
    raise ValueError("Visuals produced 0 clips - all scenes failed")
    
# This will:
# - Fail Visuals step immediately
# - Skip Editor entirely
# - Save ~5-10 minutes
# - Provide earlier feedback
```

---

## 📈 **EXPECTED RESULTS AFTER FIXES**

### With Rate Limiting Only:
```
Research ✅ (~15s)
   ↓
Script ✅ (~10min)
   ↓
Audio ✅ (~1min)
   ↓
Visuals ✅ (~5-10min) - Slower but succeeds
   ↓
Editor ✅ (~5-15min) - Renders video
   ↓
Thumbnail ✅ (~1min)
   ↓
Notify ✅

Total: ~25-40 minutes
Result: SUCCESS 🎉
```

### With Rate Limiting + Fallback:
```
Same as above, but:
- Visuals uses Pexels/static images if Nova Reel fails
- Pipeline succeeds even if Bedrock APIs are down
- More resilient to quota issues
```

### With All 3 Fixes:
```
Same as above, plus:
- Fails fast if all scenes fail
- Provides earlier feedback
- Saves time on catastrophic failures
```

---

## 🚀 **IMMEDIATE ACTIONS REQUIRED**

### 1. Add Rate Limiting to Visuals (NOW)
```bash
# Edit: lambdas/nexus-visuals/handler.py
# Add time.sleep() delays in _process_one function
# Reduce max_workers from 10 to 3
```

### 2. Rebuild and Deploy Visuals Image
```bash
docker build -t nexus-visuals:v7-rate-limit -f lambdas/nexus-visuals/Dockerfile .
docker tag nexus-visuals:v7-rate-limit 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-visuals:latest
docker push 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-visuals:latest

# Update ECS task definition
cd terraform
terraform taint module.compute.aws_ecs_task_definition.visuals
terraform apply -target=module.compute.aws_ecs_task_definition.visuals -auto-approve
```

### 3. Test Again
```bash
# Start new run
curl -X POST https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run \
  -H "Content-Type: application/json" \
  -d '{"niche":"Ancient mysteries","profile":"documentary","pipeline_type":"video","generate_shorts":false}'

# Monitor Visuals logs
aws logs tail /ecs/nexus-visuals --follow | grep -i "throttl\|error"
```

---

## 📝 **SUMMARY**

### What We Fixed (v6):
✅ Script Bedrock throttling - Rate limiting working  
✅ Inactive task definition - Rev 28 active  
✅ Editor FFmpeg crash - Validation working  

### What We Discovered:
❌ Visuals Bedrock throttling - Nova Canvas + Nova Reel both throttled  
⚠️ Empty EDL production - Consequence of throttling  
✅ Editor validation - Working perfectly!  

### What We Need to Do:
🔧 Add rate limiting to Visuals handler  
🔧 Implement fallback mechanisms  
🔧 Add early failure detection  

---

## 💡 **KEY INSIGHT**

**The pipeline is MOSTLY working!**

- Research: ✅ Working
- Script: ✅ Working (with rate limiting)
- Audio: ✅ Working
- Visuals: ❌ Throttled (needs rate limiting)
- Editor: ✅ Working (validates correctly)
- Infrastructure: ✅ Properly configured

**We're 80% there!** Just need to fix Visuals throttling.

---

**STATUS:** 🔧 **READY FOR VISUALS FIX** (v7)

**Next:** Implement rate limiting in Visuals handler + deploy

