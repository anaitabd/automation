#!/bin/bash
# Test and monitor the latest pipeline deployment

set -e

API_URL="https://qmf7zf4b4d.execute-api.us-east-1.amazonaws.com/prod"
DASHBOARD_URL="https://d2bsds71x8r1o0.cloudfront.net"

echo "════════════════════════════════════════════════════════"
echo "  Nexus Cloud Pipeline - Deployment v6 Test"
echo "════════════════════════════════════════════════════════"
echo ""

# Start pipeline run
echo "🚀 Starting test run..."
RUN_RESPONSE=$(curl -s -X POST "$API_URL/run" \
  -H "Content-Type: application/json" \
  -d '{"niche":"Ancient mysteries revealed","profile":"documentary","pipeline_type":"video","generate_shorts":false}')

RUN_ID=$(echo "$RUN_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('run_id', ''))" 2>/dev/null || echo "")

if [ -z "$RUN_ID" ]; then
    echo "❌ Failed to start pipeline run"
    echo "Response: $RUN_RESPONSE"
    exit 1
fi

echo "✅ Pipeline started!"
echo "   Run ID: $RUN_ID"
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Monitoring Links"
echo "════════════════════════════════════════════════════════"
echo ""
echo "Dashboard:"
echo "  $DASHBOARD_URL"
echo ""
echo "AWS Console:"
echo "  https://console.aws.amazon.com/states/home?region=us-east-1#/v2/executions/details/arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:$RUN_ID"
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Watch Commands"
echo "════════════════════════════════════════════════════════"
echo ""
echo "Script (rate limiting verification):"
echo "  aws logs tail /aws/lambda/nexus-script --follow | grep -i 'pass\\|sleep\\|throttl'"
echo ""
echo "Editor (EDL validation verification):"
echo "  aws logs tail /ecs/nexus-editor --follow | grep -i 'edl\\|scenes\\|error'"
echo ""
echo "Visuals (Nova Reel debugging):"
echo "  aws logs tail /ecs/nexus-visuals --follow | grep -i 'nova\\|clip\\|manifest'"
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Real-time Monitoring"
echo "════════════════════════════════════════════════════════"
echo ""

# Monitor loop
LAST_STEP=""
START_TIME=$(date +%s)

while true; do
    sleep 10

    STATUS_RESPONSE=$(curl -s "$API_URL/status/$RUN_ID")

    STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', ''))" 2>/dev/null || echo "")
    CURRENT_STEP=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('current_step', ''))" 2>/dev/null || echo "")
    PROGRESS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('progress_pct', 0))" 2>/dev/null || echo "0")

    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))

    if [ "$CURRENT_STEP" != "$LAST_STEP" ]; then
        echo "[$(date '+%H:%M:%S')] ${ELAPSED}s - Status: $STATUS | Step: $CURRENT_STEP | Progress: ${PROGRESS}%"
        LAST_STEP="$CURRENT_STEP"
    fi

    if [ "$STATUS" = "SUCCEEDED" ]; then
        echo ""
        echo "════════════════════════════════════════════════════════"
        echo "  ✅ PIPELINE SUCCEEDED!"
        echo "════════════════════════════════════════════════════════"
        echo "Total time: ${ELAPSED}s"
        echo ""
        echo "Check final video:"
        echo "  aws s3 ls s3://nexus-outputs/$RUN_ID/review/final_video.mp4"
        echo ""
        exit 0
    fi

    if [ "$STATUS" = "FAILED" ]; then
        echo ""
        echo "════════════════════════════════════════════════════════"
        echo "  ❌ PIPELINE FAILED"
        echo "════════════════════════════════════════════════════════"
        echo "Total time: ${ELAPSED}s"
        echo ""
        echo "Check error details:"
        echo "  aws stepfunctions describe-execution \\"
        echo "    --execution-arn \"arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:$RUN_ID\" \\"
        echo "    --query '{Status:status,Error:error,Cause:cause}'"
        echo ""
        echo "Check error logs:"
        echo "  aws s3 cp s3://nexus-outputs/$RUN_ID/errors/pipeline.json -"
        echo ""
        exit 1
    fi
done

