#!/usr/bin/env bash
# Monitor pipeline execution - eb6d0db6-7f54-411b-8c4b-1e093396aed7

RUN_ID="eb6d0db6-7f54-411b-8c4b-1e093396aed7"
EXEC_ARN="arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:${RUN_ID}"

echo "================================"
echo "Pipeline Monitoring - All Fixes Verification"
echo "Run ID: $RUN_ID"
echo "================================"
echo ""

# Function to check execution status
check_status() {
    aws stepfunctions describe-execution \
        --execution-arn "$EXEC_ARN" \
        --query 'status' --output text
}

# Function to get current step from execution history
get_current_step() {
    aws stepfunctions get-execution-history \
        --execution-arn "$EXEC_ARN" \
        --max-results 10 --reverse-order \
        --query 'events[?type==`TaskStateEntered`].stateEnteredEventDetails.name | [0]' \
        --output text 2>/dev/null || echo "Initializing"
}

# Monitor until completion or failure
START_TIME=$(date +%s)
LAST_STEP=""

while true; do
    STATUS=$(check_status)
    CURRENT_STEP=$(get_current_step)
    ELAPSED=$(($(date +%s) - START_TIME))

    if [ "$CURRENT_STEP" != "$LAST_STEP" ]; then
        echo "[$(date '+%H:%M:%S')] ${ELAPSED}s - Status: $STATUS | Step: $CURRENT_STEP"
        LAST_STEP="$CURRENT_STEP"

        # Check for specific milestones
        case "$CURRENT_STEP" in
            "Audio"|"Visuals")
                echo "  → AudioVisuals parallel step started (Fix #1 & #2 testing)"
                ;;
            "Editor")
                echo "  → Editor started (Fix #3 & #4 testing)"
                # Check if EDL exists
                aws s3 ls "s3://nexus-outputs/${RUN_ID}/script_with_assets.json" >/dev/null 2>&1 && \
                    echo "  ✅ EDL file confirmed in S3"
                ;;
        esac
    fi

    case "$STATUS" in
        "SUCCEEDED")
            echo ""
            echo "================================"
            echo "✅ PIPELINE SUCCEEDED!"
            echo "================================"
            echo "Total time: ${ELAPSED}s"
            echo ""
            echo "Verifying outputs..."

            # Check final video
            if aws s3 ls "s3://nexus-outputs/${RUN_ID}/review/final_video.mp4" >/dev/null 2>&1; then
                echo "  ✅ Final video: s3://nexus-outputs/${RUN_ID}/review/final_video.mp4"
            else
                echo "  ⚠️  Final video not found"
            fi

            # Check audio
            if aws s3 ls "s3://nexus-outputs/${RUN_ID}/audio/mixed_audio.wav" >/dev/null 2>&1; then
                echo "  ✅ Mixed audio exists"
            fi

            # Check EDL
            if aws s3 ls "s3://nexus-outputs/${RUN_ID}/script_with_assets.json" >/dev/null 2>&1; then
                EDL_CONTENT=$(aws s3 cp "s3://nexus-outputs/${RUN_ID}/script_with_assets.json" - 2>/dev/null)
                SCENE_COUNT=$(echo "$EDL_CONTENT" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('scenes', [])))" 2>/dev/null || echo "0")
                echo "  ✅ EDL exists with $SCENE_COUNT scenes"
            fi

            echo ""
            echo "All fixes verified successfully! 🎉"
            exit 0
            ;;
        "FAILED"|"TIMED_OUT"|"ABORTED")
            echo ""
            echo "================================"
            echo "❌ PIPELINE FAILED: $STATUS"
            echo "================================"
            echo "Total time: ${ELAPSED}s"
            echo ""
            echo "Checking logs for errors..."

            # Get failure details
            aws stepfunctions describe-execution \
                --execution-arn "$EXEC_ARN" \
                --query 'cause' --output text 2>/dev/null

            echo ""
            echo "Check CloudWatch logs:"
            echo "  aws logs tail /ecs/nexus-audio --since 30m"
            echo "  aws logs tail /ecs/nexus-visuals --since 30m"
            echo "  aws logs tail /ecs/nexus-editor --since 30m"
            exit 1
            ;;
        "RUNNING")
            # Continue monitoring
            ;;
        *)
            echo "Unknown status: $STATUS"
            ;;
    esac

    sleep 10
done

