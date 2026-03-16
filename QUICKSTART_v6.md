# 🚀 QUICK START - Test Deployment v6

## Run This Command Now:

```bash
cd /Users/abdallahnait/Documents/GitHub/automation && bash test_deployment_v6.sh
```

---

## What This Does:

1. ✅ Starts a pipeline run
2. 📊 Monitors progress every 10 seconds
3. 🎯 Shows real-time status updates
4. 📝 Provides log viewing commands
5. ✅ Reports success or failure

---

## Expected Timeline:

- **00:00 - 00:15** → Research step
- **00:15 - 11:15** → Script step (with rate limiting delays)
- **11:15 - 12:15** → Audio step
- **12:15 - 14:00** → Visuals step
- **14:00 - 29:00** → Editor step (if clips available) OR fails gracefully
- **29:00 - 30:00** → Thumbnail + Notify

**Total:** ~20-30 minutes

---

## What to Watch For:

### ✅ SUCCESS Indicators:
- Script completes all 7 passes without ThrottlingException
- Audio completes successfully
- Visuals produces video clips (or completes with 0 clips)
- Editor renders video (if clips available) OR fails with clear message
- Final video appears in S3

### ⚠️ EXPECTED FAILURE (Nova Reel 0 clips):
- Editor step fails with: "Empty EDL: 0 scenes available for rendering"
- NotifyError sends Discord notification
- CloudWatch shows clear diagnostic message

**This is GOOD** - means our validation is working!

---

## Manual Monitoring:

### Dashboard (UI):
https://d2bsds71x8r1o0.cloudfront.net

### AWS Console:
After getting RUN_ID from script output:
```
https://console.aws.amazon.com/states/home?region=us-east-1#/v2/executions/details/arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:{RUN_ID}
```

### CloudWatch Logs:
```bash
# Script step (verify rate limiting)
aws logs tail /aws/lambda/nexus-script --follow | grep -i "pass\|throttl"

# Editor step (verify EDL validation)
aws logs tail /ecs/nexus-editor --follow | grep -i "edl\|scenes\|error"

# Visuals step (debug Nova Reel)
aws logs tail /ecs/nexus-visuals --follow | grep -i "nova\|clip\|manifest"
```

---

## Alternative: Manual API Call

If you prefer to trigger manually:

```bash
curl -X POST https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/run \
  -H "Content-Type: application/json" \
  -d '{
    "niche": "Ancient mysteries revealed",
    "profile": "documentary",
    "pipeline_type": "video",
    "generate_shorts": false
  }'
```

Then check status:
```bash
RUN_ID="<your-run-id>"
curl -s https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod/status/$RUN_ID | jq .
```

---

## What's Been Fixed:

✅ **EDL Validation** - Editor checks for empty scenes before rendering  
✅ **Rate Limiting** - 5s delays between Script passes prevent throttling  
✅ **State Machine** - Updated to reference latest task definition (rev 28)  
✅ **Error Messages** - Clear, actionable diagnostics  
✅ **FFmpeg Handling** - Try/catch with fallback  

---

## Files to Review:

- **Full deployment guide:** `DEPLOYMENT_COMPLETE_v6.md`
- **Implementation summary:** `IMPLEMENTATION_SUMMARY_v6.md`
- **Test script:** `test_deployment_v6.sh`

---

**Ready? Run this now:**

```bash
cd /Users/abdallahnait/Documents/GitHub/automation && bash test_deployment_v6.sh
```

Then grab some coffee ☕ and watch the magic happen! 🎉

