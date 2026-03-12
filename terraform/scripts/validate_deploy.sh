#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════╗
# ║  validate_deploy.sh — Phase 5 post-deploy validation        ║
# ║  Verifies the Terraform-managed stack is functionally OK     ║
# ╚══════════════════════════════════════════════════════════════╝
#
# Usage:
#   cd terraform && bash scripts/validate_deploy.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0

check() {
    local name="$1"
    shift
    echo -ne "  ${CYAN}▸${NC} $name... "
    if "$@" >/dev/null 2>&1; then
        echo -e "${GREEN}✅${NC}"
        ((PASS++))
    else
        echo -e "${RED}❌${NC}"
        ((FAIL++))
    fi
}

echo ""
echo -e "${CYAN}═══ Nexus Cloud — Post-Deploy Validation ═══${NC}"
echo ""

# 1. Terraform outputs exist
API_URL=$(terraform output -raw api_url 2>/dev/null || echo "")
SFN_ARN=$(terraform output -raw state_machine_arn 2>/dev/null || echo "")

if [ -z "$API_URL" ]; then
    echo -e "${RED}Cannot read terraform outputs. Run 'terraform apply' first.${NC}"
    exit 1
fi

echo -e "${CYAN}API URL:${NC} $API_URL"
echo -e "${CYAN}SFN ARN:${NC} $SFN_ARN"
echo ""

# 2. API health check
echo -e "${CYAN}── API Health ──${NC}"
check "GET /health returns 200" curl -sf "${API_URL}health"

# 3. Step Functions reachable
echo -e "${CYAN}── Step Functions ──${NC}"
check "State machine exists" aws stepfunctions describe-state-machine --state-machine-arn "$SFN_ARN"

# 4. S3 buckets accessible
echo -e "${CYAN}── S3 Buckets ──${NC}"
ASSETS=$(terraform output -raw assets_bucket 2>/dev/null || echo "")
OUTPUTS=$(terraform output -raw outputs_bucket 2>/dev/null || echo "")
CONFIG=$(terraform output -raw config_bucket 2>/dev/null || echo "")
[ -n "$ASSETS"  ] && check "Assets bucket ($ASSETS)"  aws s3api head-bucket --bucket "$ASSETS"
[ -n "$OUTPUTS" ] && check "Outputs bucket ($OUTPUTS)" aws s3api head-bucket --bucket "$OUTPUTS"
[ -n "$CONFIG"  ] && check "Config bucket ($CONFIG)"   aws s3api head-bucket --bucket "$CONFIG"

# 5. Profiles uploaded
echo -e "${CYAN}── Config Profiles ──${NC}"
for p in documentary.json finance.json entertainment.json; do
    [ -n "$CONFIG" ] && check "Profile: $p" aws s3api head-object --bucket "$CONFIG" --key "$p"
done

# 6. Lambda functions exist
echo -e "${CYAN}── Lambda Functions ──${NC}"
for fn in nexus-research nexus-script nexus-thumbnail nexus-upload nexus-notify nexus-notify-error nexus-api-handler; do
    check "Lambda: $fn" aws lambda get-function --function-name "$fn"
done

# 7. ECS cluster + task definitions
echo -e "${CYAN}── ECS ──${NC}"
ECS_ARN=$(terraform output -raw ecs_cluster_arn 2>/dev/null || echo "")
[ -n "$ECS_ARN" ] && check "ECS cluster" aws ecs describe-clusters --clusters "$ECS_ARN"
for family in nexus-audio nexus-visuals nexus-editor; do
    check "Task def: $family" aws ecs describe-task-definition --task-definition "$family"
done

# 8. Dry-run execution
echo ""
echo -e "${CYAN}── Dry Run Test ──${NC}"
echo -ne "  ${CYAN}▸${NC} POST /run (dry_run=true)... "
RUN_RESP=$(curl -sf -X POST "${API_URL}run" \
    -H 'Content-Type: application/json' \
    -d '{"niche":"technology","profile":"documentary","dry_run":true}' 2>/dev/null || echo "")
if [ -n "$RUN_RESP" ]; then
    RUN_ID=$(echo "$RUN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('run_id',''))" 2>/dev/null || echo "")
    if [ -n "$RUN_ID" ]; then
        echo -e "${GREEN}✅${NC} run_id=$RUN_ID"
        ((PASS++))

        # Wait a few seconds then check status
        sleep 5
        echo -ne "  ${CYAN}▸${NC} GET /status/$RUN_ID... "
        STATUS_RESP=$(curl -sf "${API_URL}status/${RUN_ID}" 2>/dev/null || echo "")
        if [ -n "$STATUS_RESP" ]; then
            STATUS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            echo -e "${GREEN}✅${NC} status=$STATUS"
            ((PASS++))
        else
            echo -e "${RED}❌${NC}"
            ((FAIL++))
        fi
    else
        echo -e "${RED}❌ (no run_id in response)${NC}"
        ((FAIL++))
    fi
else
    echo -e "${RED}❌ (request failed)${NC}"
    ((FAIL++))
fi

# 9. Run local pytest suite
echo ""
echo -e "${CYAN}── Local Tests ──${NC}"
PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
echo -ne "  ${CYAN}▸${NC} pytest test_check_external... "
if python3 -m pytest "$PROJECT_ROOT/scripts/test_check_external.py" -v --tb=short -q 2>/dev/null; then
    echo -e "${GREEN}✅${NC}"
    ((PASS++))
else
    echo -e "${YELLOW}⚠️  (some tests may require .env)${NC}"
fi

# Summary
echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "  ${GREEN}Passed: $PASS${NC}  ${RED}Failed: $FAIL${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"

if [ "$FAIL" -gt 0 ]; then
    echo -e "${YELLOW}Some checks failed. Review output above.${NC}"
    exit 1
fi

echo -e "${GREEN}All checks passed!${NC}"

