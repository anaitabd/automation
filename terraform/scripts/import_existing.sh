#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════╗
# ║  import_existing.sh — Import pre-existing AWS resources     ║
# ║  into Terraform state so terraform apply stops erroring     ║
# ╚══════════════════════════════════════════════════════════════╝
#
# Usage:  cd terraform && bash scripts/import_existing.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[import]${NC} $*"; }
ok()   { echo -e "${GREEN}[  ✅  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[  ⚠️  ]${NC} $*"; }
fail() { echo -e "${RED}[  ❌  ]${NC} $*"; }

try_import() {
    local addr="$1"
    local id="$2"
    # Skip if already in state
    if terraform state show "$addr" &>/dev/null; then
        ok "SKIP $addr (already in state)"
        return 0
    fi
    log "Importing $addr ← $id"
    if terraform import -input=false "$addr" "$id" 2>&1 | tail -1; then
        ok "Imported $addr"
    else
        warn "Failed to import $addr — may not exist or wrong ID"
    fi
}

# ── Resolve account/region ──
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
log "Account: $ACCOUNT  Region: $REGION"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 1: Secrets Manager (secrets + versions)"
echo "═══════════════════════════════════════════════════════════"

for secret in \
    "module.secrets.aws_secretsmanager_secret.perplexity|nexus/perplexity_api_key" \
    "module.secrets.aws_secretsmanager_secret.elevenlabs|nexus/elevenlabs_api_key" \
    "module.secrets.aws_secretsmanager_secret.pexels|nexus/pexels_api_key" \
    "module.secrets.aws_secretsmanager_secret.freesound|nexus/freesound_api_key" \
    "module.secrets.aws_secretsmanager_secret.youtube|nexus/youtube_credentials" \
    "module.secrets.aws_secretsmanager_secret.discord|nexus/discord_webhook_url" \
    "module.secrets.aws_secretsmanager_secret.db|nexus/db_credentials"; do
    IFS='|' read -r addr id <<< "$secret"
    try_import "$addr" "$id"
done

# Secret versions — get AWSCURRENT version ID for each
for sv in \
    "perplexity|nexus/perplexity_api_key" \
    "elevenlabs|nexus/elevenlabs_api_key" \
    "pexels|nexus/pexels_api_key" \
    "freesound|nexus/freesound_api_key" \
    "youtube|nexus/youtube_credentials" \
    "discord|nexus/discord_webhook_url" \
    "db|nexus/db_credentials"; do
    TF_KEY=$(echo "$sv" | cut -d'|' -f1)
    SECRET_NAME=$(echo "$sv" | cut -d'|' -f2)
    VERSION_ID=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
        --query 'VersionIdsToStages | to_entries(@) | [?contains(value, `AWSCURRENT`)] | [0].key' \
        --output text 2>/dev/null || echo "")
    if [ -n "$VERSION_ID" ] && [ "$VERSION_ID" != "None" ]; then
        try_import \
            "module.secrets.aws_secretsmanager_secret_version.${TF_KEY}" \
            "${SECRET_NAME}|${VERSION_ID}"
    else
        warn "No AWSCURRENT version for ${SECRET_NAME} — will be created"
    fi
done

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 2: Identity — IAM Roles"
echo "═══════════════════════════════════════════════════════════"

try_import "module.identity.aws_iam_role.research"      "nexus-research-role"
try_import "module.identity.aws_iam_role.script"         "nexus-script-role"
try_import "module.identity.aws_iam_role.thumbnail"      "nexus-thumbnail-role"
try_import "module.identity.aws_iam_role.upload"         "nexus-upload-role"
try_import "module.identity.aws_iam_role.notify"         "nexus-notify-role"
try_import "module.identity.aws_iam_role.ecs_execution"  "nexus-ecs-task-execution-role"
try_import "module.identity.aws_iam_role.ecs_task"       "nexus-ecs-task-role"
try_import "module.identity.aws_iam_role.mediaconvert"   "nexus-mediaconvert-role"
try_import "module.identity.aws_iam_role.sfn"            "nexus-sfn-role"
try_import "module.identity.aws_iam_role.api"            "nexus-api-role"

# IAM inline policies (import format: role_name:policy_name)
try_import "module.identity.aws_iam_role_policy.research"            "nexus-research-role:nexus-research-policy"
try_import "module.identity.aws_iam_role_policy.script"              "nexus-script-role:nexus-script-policy"
try_import "module.identity.aws_iam_role_policy.thumbnail"           "nexus-thumbnail-role:nexus-thumbnail-policy"
try_import "module.identity.aws_iam_role_policy.upload"              "nexus-upload-role:nexus-upload-policy"
try_import "module.identity.aws_iam_role_policy.notify"              "nexus-notify-role:nexus-notify-policy"
try_import "module.identity.aws_iam_role_policy.api"                 "nexus-api-role:nexus-api-policy"
try_import "module.identity.aws_iam_role_policy.ecs_execution_secrets" "nexus-ecs-task-execution-role:nexus-ecs-execution-secrets"
try_import "module.identity.aws_iam_role_policy.ecs_task"            "nexus-ecs-task-role:nexus-ecs-task-policy"
try_import "module.identity.aws_iam_role_policy.mediaconvert"        "nexus-mediaconvert-role:nexus-mediaconvert-s3"
try_import "module.identity.aws_iam_role_policy.sfn"                 "nexus-sfn-role:nexus-sfn-policy"

# IAM managed policy attachments (import format: role-name/policy-arn)
LAMBDA_BASIC="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
try_import "module.identity.aws_iam_role_policy_attachment.research_basic"       "nexus-research-role/${LAMBDA_BASIC}"
try_import "module.identity.aws_iam_role_policy_attachment.script_basic"         "nexus-script-role/${LAMBDA_BASIC}"
try_import "module.identity.aws_iam_role_policy_attachment.thumbnail_basic"      "nexus-thumbnail-role/${LAMBDA_BASIC}"
try_import "module.identity.aws_iam_role_policy_attachment.upload_basic"         "nexus-upload-role/${LAMBDA_BASIC}"
try_import "module.identity.aws_iam_role_policy_attachment.notify_basic"         "nexus-notify-role/${LAMBDA_BASIC}"
try_import "module.identity.aws_iam_role_policy_attachment.api_basic"            "nexus-api-role/${LAMBDA_BASIC}"
try_import "module.identity.aws_iam_role_policy_attachment.ecs_execution_basic"  "nexus-ecs-task-execution-role/${LAMBDA_BASIC}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 3: Compute — Lambda, ECS, ECR, Log Groups"
echo "═══════════════════════════════════════════════════════════"

# Lambda functions
for fn in research script thumbnail upload notify; do
    try_import "module.compute.aws_lambda_function.${fn}" "nexus-${fn}"
done
try_import "module.compute.aws_lambda_function.notify_error" "nexus-notify-error"
try_import "module.compute.aws_lambda_function.api_handler"  "nexus-api-handler"

# ECS Cluster
try_import "module.compute.aws_ecs_cluster.main" \
    "arn:aws:ecs:${REGION}:${ACCOUNT}:cluster/nexus-video-cluster"

# ECR repositories
for repo in audio visuals editor shorts; do
    try_import "module.compute.aws_ecr_repository.${repo}" "nexus-${repo}"
done

# CloudWatch Log Groups
for lg in audio visuals editor shorts; do
    try_import "module.compute.aws_cloudwatch_log_group.${lg}" "/ecs/nexus-${lg}"
done

# ECS Task Definitions (import latest revision ARN)
for td in audio visuals editor; do
    TD_ARN=$(aws ecs describe-task-definition --task-definition "nexus-${td}" \
        --query 'taskDefinition.taskDefinitionArn' --output text 2>/dev/null || echo "")
    if [ -n "$TD_ARN" ] && [ "$TD_ARN" != "None" ]; then
        try_import "module.compute.aws_ecs_task_definition.${td}" "$TD_ARN"
    else
        warn "nexus-${td} task definition not found — will be created"
    fi
done
# nexus-shorts task def doesn't exist yet — skip

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 4: Networking — EFS"
echo "═══════════════════════════════════════════════════════════"

EFS_ID=$(aws efs describe-file-systems \
    --query "FileSystems[?Tags[?Key=='Name' && Value=='nexus-scratch']].FileSystemId | [0]" \
    --output text 2>/dev/null || echo "")
if [ -z "$EFS_ID" ] || [ "$EFS_ID" = "None" ]; then
    # Fallback: try known ID
    EFS_ID="fs-09ee6fd5a4b414c54"
fi
try_import "module.networking.aws_efs_file_system.scratch" "$EFS_ID"

# EFS Security Group
SG_ID=$(aws efs describe-mount-targets --file-system-id "$EFS_ID" \
    --query 'MountTargets[0].MountTargetId' --output text 2>/dev/null || echo "")
if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
    # Get security group from mount target
    MT_SG=$(aws efs describe-mount-target-security-groups --mount-target-id "$SG_ID" \
        --query 'SecurityGroups[0]' --output text 2>/dev/null || echo "")
    if [ -n "$MT_SG" ] && [ "$MT_SG" != "None" ]; then
        try_import "module.networking.aws_security_group.efs" "$MT_SG"
    fi
fi

# EFS Access Point
AP_ID=$(aws efs describe-access-points --file-system-id "$EFS_ID" \
    --query 'AccessPoints[0].AccessPointId' --output text 2>/dev/null || echo "")
if [ -n "$AP_ID" ] && [ "$AP_ID" != "None" ]; then
    try_import "module.networking.aws_efs_access_point.scratch" "$AP_ID"
else
    warn "EFS access point not found — will be created"
fi

# EFS Mount Targets (keyed by subnet ID)
while IFS=$'\t' read -r MT_ID SUBNET_ID; do
    if [ -n "$MT_ID" ] && [ "$MT_ID" != "None" ]; then
        try_import \
            "module.networking.aws_efs_mount_target.scratch[\"${SUBNET_ID}\"]" \
            "$MT_ID"
    fi
done < <(aws efs describe-mount-targets --file-system-id "$EFS_ID" \
    --query 'MountTargets[*].[MountTargetId,SubnetId]' --output text 2>/dev/null)

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 5: Orchestration — Step Functions"
echo "═══════════════════════════════════════════════════════════"

SFN_ARN="arn:aws:states:${REGION}:${ACCOUNT}:stateMachine:nexus-pipeline"
try_import "module.orchestration.aws_sfn_state_machine.pipeline" "$SFN_ARN"

# SFN log group (if exists)
SFN_LG=$(aws logs describe-log-groups \
    --log-group-name-prefix "/aws/vendedlogs/states/nexus-pipeline" \
    --query 'logGroups[0].logGroupName' --output text 2>/dev/null || echo "")
if [ -n "$SFN_LG" ] && [ "$SFN_LG" != "None" ]; then
    try_import "module.orchestration.aws_cloudwatch_log_group.sfn" "$SFN_LG"
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 6: Storage — S3"
echo "═══════════════════════════════════════════════════════════"

try_import "module.storage.aws_s3_bucket.dashboard" "nexus-dashboard-${ACCOUNT}"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 7: Observability — EventBridge"
echo "═══════════════════════════════════════════════════════════"

try_import "module.observability.aws_cloudwatch_event_rule.schedule" "nexus-pipeline-schedule"
try_import "module.observability.aws_iam_role.events" "nexus-events-sfn-role"

# EventBridge target
TARGET_ID=$(aws events list-targets-by-rule --rule nexus-pipeline-schedule \
    --query 'Targets[0].Id' --output text 2>/dev/null || echo "")
if [ -n "$TARGET_ID" ] && [ "$TARGET_ID" != "None" ]; then
    try_import "module.observability.aws_cloudwatch_event_target.schedule" \
        "nexus-pipeline-schedule/${TARGET_ID}"
fi

# EventBridge IAM policy
try_import "module.observability.aws_iam_role_policy.events_sfn" \
    "nexus-events-sfn-role:permissions"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Phase 8: API Gateway"
echo "═══════════════════════════════════════════════════════════"

# API Gateway REST API is already in state; deployments/stages are new resources

echo ""
ok "═══════════════════════════════════════════════════════════"
ok "  Import complete! Now run:  terraform plan"
ok "═══════════════════════════════════════════════════════════"
