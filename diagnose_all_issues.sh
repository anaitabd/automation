#!/bin/bash
# Comprehensive issue diagnostic for Nexus Cloud Pipeline

echo "════════════════════════════════════════════════════════"
echo "  NEXUS CLOUD - ALL CURRENT ISSUES"
echo "════════════════════════════════════════════════════════"
echo ""

# Check infrastructure
echo "1. INFRASTRUCTURE STATUS"
echo "─────────────────────────────────────────────────────────"
echo ""
echo "Editor Task Definition:"
aws ecs describe-task-definition --task-definition nexus-editor \
  --query 'taskDefinition.{Revision:revision,Status:status}' 2>/dev/null

echo ""
echo "Recent Executions:"
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline \
  --max-results 10 \
  --query 'executions[].{Name:name,Status:status,Start:startDate}' 2>/dev/null

echo ""
echo "2. RECENT ERRORS"
echo "─────────────────────────────────────────────────────────"
echo ""
echo "Error files in S3:"
aws s3 ls s3://nexus-outputs/ --recursive | grep 'errors/' | tail -10

echo ""
echo "3. LATEST FAILED RUN DETAILS"
echo "─────────────────────────────────────────────────────────"
LATEST_FAILED=$(aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:670294435884:stateMachine:nexus-pipeline \
  --status-filter FAILED \
  --max-results 1 \
  --query 'executions[0].executionArn' \
  --output text 2>/dev/null)

if [ -n "$LATEST_FAILED" ] && [ "$LATEST_FAILED" != "None" ]; then
    echo "Latest failed execution: $LATEST_FAILED"
    echo ""
    aws stepfunctions describe-execution \
      --execution-arn "$LATEST_FAILED" \
      --query '{Status:status,StartDate:startDate,StopDate:stopDate}' 2>/dev/null
    echo ""
    echo "Error cause:"
    aws stepfunctions describe-execution \
      --execution-arn "$LATEST_FAILED" \
      --query 'cause' \
      --output text 2>/dev/null | head -20
fi

echo ""
echo "4. CHECK LATEST LOGS"
echo "─────────────────────────────────────────────────────────"
echo ""
echo "Recent Script errors:"
aws logs filter-log-events \
  --log-group-name /aws/lambda/nexus-script \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern "ERROR" \
  --max-items 5 \
  --query 'events[].message' \
  --output text 2>/dev/null | head -10

echo ""
echo "Recent Editor errors:"
aws logs filter-log-events \
  --log-group-name /ecs/nexus-editor \
  --start-time $(($(date +%s) - 3600))000 \
  --filter-pattern "ERROR" \
  --max-items 5 \
  --query 'events[].message' \
  --output text 2>/dev/null | head -10

echo ""
echo "════════════════════════════════════════════════════════"
echo "  DIAGNOSTIC COMPLETE"
echo "════════════════════════════════════════════════════════"

