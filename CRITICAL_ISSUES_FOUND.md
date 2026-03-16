# CRITICAL ISSUES FOUND - Final Fix Required

## Diagnostic Results Analysis

### ❌ **Issue #1: Nova Reel Videos Not Generated**
**Evidence:**
```
Scenes: 0
```
- 12 scene images created ✅
- 12 manifest.json files (53 bytes each)
- **0 actual .mp4 video files** ❌

**Root Cause:** Nova Reel jobs are failing. The TEXT_TO_VIDEO fix didn't work.

**Need to investigate:** Check one of the manifest files to see the actual error from Nova Reel.

---

### ❌ **Issue #2: ECS Not Using New Docker Images**
**Evidence:**
```
2026-03-15T20:56:06 [nexus-editor] FATAL: Error: You passed /app/src/index.tsx as your entry point, but this file does not contain "registerRoot"
2026-03-15T20:57:18 [nexus-editor] FATAL: Error: You passed /app/src/index.tsx as your entry point, but this file does not contain "registerRoot"
2026-03-15T20:58:42 [nexus-editor] FATAL: Error: You passed /app/src/index.tsx as your entry point, but this file does not contain "registerRoot"
```

**Root Cause:** ECS Fargate is caching the old image. Even though we pushed new images to ECR, ECS didn't pull them.

**Fix Required:** Force ECS task definition updates or explicitly tag images with versions.

---

### ❌ **Issue #3: Remotion File Path Error (New Issue)**
**Evidence:**
```
2026-03-15T21:24:54 [nexus-editor] FATAL: Error: Error while downloading file:///mnt/scratch/nexus-render-pAD6tv/narration.mp3: 
Error: Can only download URLs starting with http:// or https://, got "file:///mnt/scratch/nexus-render-pAD6tv/narration.mp3"
```

**Root Cause:** The Editor is passing local file paths to Remotion, but Remotion expects HTTP URLs or relative paths.

**Fix Required:** Update the Editor's render.js to use relative paths instead of file:// URLs.

---

## Immediate Actions Required

### Action #1: Force ECS to Pull New Images

**Option A: Update Task Definitions (Recommended)**
```bash
cd /Users/abdallahnait/Documents/GitHub/automation/terraform

# Force recreation of task definitions
terraform taint module.compute.aws_ecs_task_definition.editor
terraform taint module.compute.aws_ecs_task_definition.visuals
terraform apply -auto-approve
```

**Option B: Use Image Digests Instead of :latest Tag**
```bash
# Get current digest
aws ecr describe-images \
  --repository-name nexus-editor \
  --image-ids imageTag=latest \
  --query 'imageDetails[0].imageDigest' \
  --output text

# Update task definition to use digest:
# 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor@sha256:0b48dd58...
```

**Option C: Stop All Running Tasks (Force Fresh Start)**
```bash
# List running tasks
aws ecs list-tasks \
  --cluster nexus-video-cluster \
  --family nexus-editor

# Stop them (ECS will restart with new image)
aws ecs stop-task \
  --cluster nexus-video-cluster \
  --task TASK_ID
```

---

### Action #2: Fix Remotion File Path Issue

**File:** `lambdas/nexus-editor/render.js`

**Find:**
```javascript
audioPath: `file://${audioLocalPath}`
```

**Replace with:**
```javascript
audioPath: path.relative(workDir, audioLocalPath)
```

**Or use Remotion's staticFile():**
```javascript
import { staticFile } from 'remotion';
audioPath: staticFile(path.basename(audioLocalPath))
```

---

### Action #3: Investigate Nova Reel Failure

**Check manifest content:**
```bash
aws s3 cp s3://nexus-outputs/eb6d0db6-7f54-411b-8c4b-1e093396aed7/clips/scene_001/jv9lvpsvjcb3/manifest.json - | jq '.'
```

**Possible issues:**
1. TEXT_TO_VIDEO might need different parameters
2. Async invoke might be failing silently
3. S3 output path might be incorrect
4. Images might not be accessible to Nova Reel

---

## Quick Fix Script

```bash
#!/bin/bash
set -e

cd /Users/abdallahnait/Documents/GitHub/automation

echo "=== APPLYING CRITICAL FIXES ==="

# 1. Fix Remotion file path issue
echo "1. Fixing Remotion file path..."
# (Manual edit required - see Action #2)

# 2. Rebuild Editor with fix
echo "2. Rebuilding Editor..."
docker build --platform linux/arm64 \
  -f lambdas/nexus-editor/Dockerfile \
  -t 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:v2 \
  .

# 3. Push with NEW tag (not :latest)
echo "3. Pushing with new tag..."
docker push 670294435884.dkr.ecr.us-east-1.amazonaws.com/nexus-editor:v2

# 4. Update task definition to use v2
echo "4. Updating task definition..."
# (Terraform or AWS CLI to update image tag)

# 5. Investigate Nova Reel
echo "5. Checking Nova Reel manifests..."
aws s3 cp s3://nexus-outputs/eb6d0db6-7f54-411b-8c4b-1e093396aed7/clips/scene_001/jv9lvpsvjcb3/manifest.json -

echo "=== FIXES COMPLETE ==="
```

---

## Why ECS Didn't Pull New Images

**ECS Image Pull Behavior:**
- When using `:latest` tag, ECS caches the image
- Even if you push a new `:latest`, ECS may not pull it
- Need to either:
  1. Use versioned tags (`:v1`, `:v2`, etc.)
  2. Force task definition recreation
  3. Use image digests (`@sha256:...`)

**Our mistake:** We pushed to `:latest` but didn't force ECS to re-pull.

---

## Summary

| Issue | Status | Fix Needed |
|-------|--------|------------|
| Nova Reel failing | ❌ Active | Investigate manifest errors |
| ECS using old images | ❌ Active | Force task def update or use versioned tags |
| Remotion file paths | ❌ Active | Change file:// to relative paths |
| registerRoot fix | ⚠️ Not deployed | See Issue #2 |
| EDL_S3_KEY | ✅ Working | Confirmed in logs |

---

## Next Steps

1. **Fix Remotion file paths** in render.js
2. **Force ECS to pull new images** via task definition update
3. **Investigate Nova Reel** manifest errors
4. **Rebuild and redeploy** with versioned tags (`:v2`)
5. **Run fresh test** to verify all fixes

---

**Priority:** HIGH - Multiple blockers preventing pipeline success
**Estimated Time:** 30-60 minutes for all fixes

