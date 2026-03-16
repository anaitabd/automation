# ✅ ISSUE CHECKLIST - Quick Action Guide

**Run these commands to verify each issue:**

---

## 1️⃣ FIXED ISSUES (Should Pass) ✅

### Test: Bedrock Throttling Fixed
```bash
# Monitor Script step for throttling errors
aws logs tail /aws/lambda/nexus-script --follow | grep -i "throttl\|error"
# Expected: NO ThrottlingException errors
```

### Test: Task Definition Updated
```bash
# Check current revision
aws ecs describe-task-definition --task-definition nexus-editor \
  --query 'taskDefinition.{Revision:revision,Status:status}'
# Expected: Revision: 28, Status: ACTIVE
```

### Test: EDL Validation Working
```bash
# Start a test run and check Editor logs
aws logs tail /ecs/nexus-editor --follow | grep -i "edl\|scenes"
# Expected: Either "EDL loaded — N scenes" OR "FATAL: EDL contains 0 scenes"
```

---

## 2️⃣ ACTIVE ISSUES (Will Fail) ⚠️

### Issue 4: Nova Reel Produces 0 Clips

**Check latest run:**
```bash
# Get latest run ID
LATEST_RUN=$(aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline \
  --max-results 1 \
  --query 'executions[0].name' \
  --output text)

echo "Latest run: $LATEST_RUN"

# Check EDL
aws s3 cp s3://nexus-outputs/$LATEST_RUN/script_with_assets.json - | jq '.scenes | length'
# Expected: 0 (this is the bug)

# Check manifest files
aws s3 ls s3://nexus-outputs/$LATEST_RUN/clips/ --recursive | grep manifest
# Expected: manifest.json files exist but contain errors
```

**Test Nova Reel directly:**
```bash
# Create test invocation
aws bedrock-runtime start-async-invoke \
  --model-id amazon.nova-reel-v1:0 \
  --model-input '{"taskType":"TEXT_TO_VIDEO","textToVideoParams":{"text":"Ancient temple ruins"},"videoGenerationConfig":{"durationSeconds":6,"fps":24,"dimension":"1280x720"}}' \
  --output-data-config '{"s3OutputDataConfig":{"s3Uri":"s3://nexus-outputs/test-nova-reel/"}}' \
  --region us-east-1

# Save the invocation ARN, then check status later
# aws bedrock-runtime get-async-invoke --invocation-arn <arn>
```

---

### Issue 5: Pipeline Fails at Editor

**Check execution status:**
```bash
LATEST_RUN=$(aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline \
  --max-results 1 \
  --query 'executions[0].executionArn' \
  --output text)

aws stepfunctions describe-execution \
  --execution-arn "$LATEST_RUN" \
  --query '{Status:status,Error:error}' | jq .

# Expected: Status: FAILED, Error related to Editor
```

---

## 3️⃣ UNCONFIRMED ISSUES (Need Testing) 🔍

### Issue 6: Shorts Parameters Not Forwarded

**Test Shorts pipeline:**
```bash
# Start a Shorts-only run
SHORTS_RESPONSE=$(curl -s -X POST https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run \
  -H "Content-Type: application/json" \
  -d '{
    "niche": "Quick finance tips",
    "profile": "finance",
    "pipeline_type": "shorts",
    "generate_shorts": true,
    "shorts_tiers": "micro,short"
  }')

SHORTS_RUN=$(echo "$SHORTS_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))")

echo "Shorts run: $SHORTS_RUN"

# Check if parameters made it to execution input
sleep 5
aws stepfunctions describe-execution \
  --execution-arn "arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:$SHORTS_RUN" \
  --query 'input' | jq '{generate_shorts, shorts_tiers, channel_id}'

# Expected: If these are null/missing, Issue #6 is CONFIRMED
```

---

### Issue 7: Perplexity Fact-Check

**Check Script logs for Pass 7:**
```bash
# Filter logs for Pass 7
aws logs tail /aws/lambda/nexus-script --since 30m | grep -i "pass 7\|perplexity\|fact"

# Expected: Should see "Pass 7/7: Perplexity fact-check" message
```

**Verify API key:**
```bash
aws secretsmanager get-secret-value \
  --secret-id nexus/perplexity_api_key \
  --query 'SecretString'

# Expected: {"api_key": "..."}
```

---

## 🚀 COMPLETE TEST SEQUENCE

**Run everything in order:**

```bash
#!/bin/bash
set -e

echo "════════════════════════════════════════════════════════"
echo "  COMPREHENSIVE ISSUE TEST"
echo "════════════════════════════════════════════════════════"
echo ""

# 1. Start a video pipeline run
echo "1️⃣  Starting VIDEO pipeline test..."
VIDEO_RUN=$(curl -s -X POST https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run \
  -H "Content-Type: application/json" \
  -d '{"niche":"Ancient mysteries","profile":"documentary","pipeline_type":"video","generate_shorts":false}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))")

echo "   Video run started: $VIDEO_RUN"
echo ""

# 2. Start a shorts pipeline run
echo "2️⃣  Starting SHORTS pipeline test..."
SHORTS_RUN=$(curl -s -X POST https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run \
  -H "Content-Type: application/json" \
  -d '{"niche":"Quick tips","profile":"finance","pipeline_type":"shorts","generate_shorts":true,"shorts_tiers":"micro"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))")

echo "   Shorts run started: $SHORTS_RUN"
echo ""

# 3. Wait for Script step to complete on video run
echo "3️⃣  Waiting 15 minutes for Script step to complete..."
echo "   (You can Ctrl+C and check manually)"
sleep 900

# 4. Check results
echo ""
echo "4️⃣  Checking results..."
echo ""
echo "VIDEO run status:"
curl -s https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/status/$VIDEO_RUN | jq '{status,current_step,error}'

echo ""
echo "SHORTS run status:"
curl -s https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/status/$SHORTS_RUN | jq '{status,current_step,error}'

echo ""
echo "5️⃣  Check for Shorts parameters in execution:"
aws stepfunctions describe-execution \
  --execution-arn "arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:$SHORTS_RUN" \
  --query 'input' | jq '{generate_shorts,shorts_tiers}'

echo ""
echo "════════════════════════════════════════════════════════"
echo "  TEST COMPLETE"
echo "════════════════════════════════════════════════════════"
echo ""
echo "Monitor logs:"
echo "  Script:  aws logs tail /aws/lambda/nexus-script --follow"
echo "  Editor:  aws logs tail /ecs/nexus-editor --follow"
echo "  Visuals: aws logs tail /ecs/nexus-visuals --follow"
echo ""
```

Save this as `test_all_issues.sh` and run it.

---

## 📊 EXPECTED RESULTS

### ✅ Fixed Issues Should Show:
- No ThrottlingException in Script logs
- Task definition revision 28
- Editor validates EDL (fails gracefully if 0 scenes)

### ⚠️ Active Issues Will Show:
- Visuals produces 0 clips
- Editor fails with "Empty EDL" message
- Pipeline status: FAILED

### 🔍 Unconfirmed Issues Will Reveal:
- Shorts parameters missing → Issue #6 confirmed
- Perplexity errors → Issue #7 confirmed
- Both work → No additional issues!

---

## 📝 SUMMARY

**Quick test:** Run the video pipeline and watch for Editor error  
**Full test:** Run the complete test sequence above  
**Result:** You'll see exactly which issues are real vs. fixed

**Files created:**
- `ALL_ISSUES_LIST.md` - Comprehensive issue documentation
- `ISSUE_CHECKLIST.md` - This file (quick tests)
- `diagnose_all_issues.sh` - Diagnostic script
- `test_deployment_v6.sh` - Automated test with monitoring

**Next step:** Choose your test approach and run it! 🚀

