#!/usr/bin/env bash
echo "WARNING: CDK deploy is deprecated. Use terraform/scripts/deploy_tf.sh"
exit 1
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════╗
# ║  Nexus Cloud — Full AWS Deployment Script                   ║
# ║  Deploys everything to AWS for testing                      ║
# ╚══════════════════════════════════════════════════════════════╝

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_DIR="$PROJECT_DIR/infrastructure"
LAYERS_DIR="$INFRA_DIR/layers"

log()  { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[  ✅  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[  ⚠️  ]${NC} $*"; }
err()  { echo -e "${RED}[  ❌  ]${NC} $*"; }

# ──────────────────────────────────────────────────────────────
# 0. Load .env & pre-flight checks
# ──────────────────────────────────────────────────────────────

log "Loading .env file..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    log "No .env found — creating from env.exemple..."
    cp "$PROJECT_DIR/env.exemple" "$PROJECT_DIR/.env"
    warn ".env created — please fill in your API keys and AWS credentials, then re-run!"
    exit 1
fi

# Source .env: export every non-comment, non-empty line
set -a
while IFS= read -r line || [ -n "$line" ]; do
    # skip comments and blank lines
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" ]] && continue
    eval "$line" 2>/dev/null || true
done < "$PROJECT_DIR/.env"
set +a
ok ".env loaded"

log "Running pre-flight checks..."

command -v aws   >/dev/null 2>&1 || { err "AWS CLI not found. Install: brew install awscli"; exit 1; }
command -v node  >/dev/null 2>&1 || { err "Node.js not found. Install: brew install node"; exit 1; }
command -v npm   >/dev/null 2>&1 || { err "npm not found. Install with Node.js"; exit 1; }
command -v python3 >/dev/null 2>&1 || { err "python3 not found"; exit 1; }
command -v docker >/dev/null 2>&1 || { err "Docker not found. Install Docker Desktop"; exit 1; }

# Validate AWS credentials from .env
if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    err "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in .env"
    exit 1
fi

# Validate other required API keys
MISSING_KEYS=()
[ -z "${PERPLEXITY_API_KEY:-}"  ] && MISSING_KEYS+=("PERPLEXITY_API_KEY")
[ -z "${ELEVENLABS_API_KEY:-}"  ] && MISSING_KEYS+=("ELEVENLABS_API_KEY")
[ -z "${PEXELS_API_KEY:-}"      ] && MISSING_KEYS+=("PEXELS_API_KEY")
[ -z "${PIXABAY_API_KEY:-}"     ] && MISSING_KEYS+=("PIXABAY_API_KEY")
[ -z "${FREESOUND_API_KEY:-}"   ] && MISSING_KEYS+=("FREESOUND_API_KEY")
[ -z "${DISCORD_WEBHOOK_URL:-}" ] && MISSING_KEYS+=("DISCORD_WEBHOOK_URL")
if [ ${#MISSING_KEYS[@]} -gt 0 ]; then
    err "Missing required keys in .env:"
    for k in "${MISSING_KEYS[@]}"; do err "  • $k"; done
    exit 1
fi

export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1

# Verify credentials work
if ! aws sts get-caller-identity >/dev/null 2>&1; then
    err "AWS credentials in .env are invalid. Check AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY"
    exit 1
fi

# Resolve account ID from .env or STS
if [ -n "${AWS_ACCOUNT_ID:-}" ]; then
    ok "Using AWS_ACCOUNT_ID from .env: $AWS_ACCOUNT_ID"
else
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    ok "Resolved AWS Account ID via STS: $AWS_ACCOUNT_ID"
fi
ok "AWS Account: $AWS_ACCOUNT_ID | Region: $AWS_REGION"

# ──────────────────────────────────────────────────────────────
# 1. Install CDK CLI if needed
# ──────────────────────────────────────────────────────────────

if ! command -v cdk >/dev/null 2>&1; then
    log "Installing AWS CDK CLI..."
    npm install -g aws-cdk
fi
ok "CDK version: $(cdk --version)"

# ──────────────────────────────────────────────────────────────
# 2. Create Secrets Manager secrets via setup_aws.py
# ──────────────────────────────────────────────────────────────

log "Running AWS bootstrap (S3 buckets, Secrets Manager, IAM roles)..."
log "Building and running setup-aws via Docker Compose..."
# Pass AWS credentials from .env to docker-compose
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_REGION="$AWS_REGION" \
docker compose -f "$PROJECT_DIR/docker-compose.yml" up setup-aws --build
ok "AWS resources bootstrapped"

# ──────────────────────────────────────────────────────────────
# 2b. Connection tests — verify all services before building
# ──────────────────────────────────────────────────────────────

log "Running connection tests (AWS, external APIs, PostgreSQL)..."
if AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
   AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
   AWS_REGION="$AWS_REGION" \
   docker compose -f "$PROJECT_DIR/docker-compose.yml" run --rm --build test-connections; then
    ok "All connection tests passed!"
else
    CONN_EXIT=$?
    warn "Some connection tests failed (exit $CONN_EXIT). Review the output above."
    warn "Bedrock failures are expected if models are not yet enabled in AWS Console."
    echo ""
    read -rp "$(echo -e "${YELLOW}Continue deployment anyway? [y/N]:${NC} ")" CONTINUE_DEPLOY
    if [[ ! "$CONTINUE_DEPLOY" =~ ^[Yy]$ ]]; then
        err "Deployment aborted. Fix the failing connections and re-run."
        exit 1
    fi
    warn "Continuing deployment despite connection failures..."
fi

# Re-source .env — setup_aws.py writes back MEDIACONVERT_ROLE_ARN, bucket names, etc.
set -a
while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" ]] && continue
    eval "$line" 2>/dev/null || true
done < "$PROJECT_DIR/.env"
set +a
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION="$AWS_REGION" AWS_REGION
ok "Re-loaded .env after bootstrap"

# ──────────────────────────────────────────────────────────────
# 3. Build Lambda layers
# ──────────────────────────────────────────────────────────────

log "Building Lambda layers..."

# Use python:3.12-slim for layer builds (arm64 to match Lambda arch)
LAYER_BUILD_IMAGE="python:3.12-slim"

# --- API Layer ---
log "Building API layer (requests, boto3, psycopg2, python-dotenv, Pillow)..."
rm -rf "$LAYERS_DIR/api"
mkdir -p "$LAYERS_DIR/api/python"
API_CID=$(docker create --platform linux/arm64 "$LAYER_BUILD_IMAGE" \
    bash -c "mkdir /out && pip install --no-cache-dir requests boto3 psycopg2-binary python-dotenv json-repair Pillow -t /out && rm -rf /out/*.dist-info /out/__pycache__")
docker start -a "$API_CID"
docker cp "$API_CID":/out/. "$LAYERS_DIR/api/python/"
docker rm "$API_CID" >/dev/null
ok "API layer built (includes Pillow)"

# --- FFmpeg Layer ---
log "Building FFmpeg layer (static arm64 binaries)..."
rm -rf "$LAYERS_DIR/ffmpeg"
mkdir -p "$LAYERS_DIR/ffmpeg/bin"
FFMPEG_CID=$(docker create --platform linux/arm64 debian:bookworm-slim \
    bash -c "
        apt-get update && apt-get install -y --no-install-recommends curl xz-utils ca-certificates &&
        mkdir /out &&
        curl -L --retry 5 --retry-delay 10 --retry-all-errors \
            -o /tmp/ffmpeg.tar.xz \
            https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz &&
        tar -xJf /tmp/ffmpeg.tar.xz --strip-components=1 -C /out --wildcards '*/ffmpeg' '*/ffprobe' &&
        chmod +x /out/ffmpeg /out/ffprobe &&
        /out/ffmpeg -version
    ")
docker start -a "$FFMPEG_CID"
docker cp "$FFMPEG_CID":/out/. "$LAYERS_DIR/ffmpeg/bin/"
docker rm "$FFMPEG_CID" >/dev/null
ok "FFmpeg layer built"

# NOTE: ML layer removed — nexus-visuals and nexus-editor use Docker container
# images (Dockerfile in each lambda dir) built automatically by CDK.

# ──────────────────────────────────────────────────────────────
# 4. Copy shared pipeline utils into each Lambda directory
# ──────────────────────────────────────────────────────────────

SHARED_UTILS="$PROJECT_DIR/lambdas/nexus_pipeline_utils.py"
log "Copying shared pipeline utils into each Lambda..."
for lambda_dir in "$PROJECT_DIR"/lambdas/nexus-*/; do
    cp -f "$SHARED_UTILS" "$lambda_dir/nexus_pipeline_utils.py"
done
ok "Shared utils copied to all Lambda directories"

# ──────────────────────────────────────────────────────────────
# 5. Install CDK Python dependencies & create symlinks
# ──────────────────────────────────────────────────────────────

log "Installing CDK Python dependencies..."
cd "$INFRA_DIR"

# CDK stack references lambdas/ and statemachine/ via relative paths.
# Create symlinks inside infrastructure/ so CDK can resolve them.
ln -sfn "$PROJECT_DIR/lambdas"      "$INFRA_DIR/lambdas"
ln -sfn "$PROJECT_DIR/statemachine" "$INFRA_DIR/statemachine"
ok "Symlinks created: lambdas/, statemachine/ → infrastructure/"

# Use a separate venv for CDK (avoid conflicts with project venv)
if [ ! -d "$INFRA_DIR/.venv" ]; then
    python3 -m venv "$INFRA_DIR/.venv"
fi
source "$INFRA_DIR/.venv/bin/activate"
pip install -q -r requirements.txt
ok "CDK dependencies installed"

# ──────────────────────────────────────────────────────────────
# 6. Bootstrap CDK (first-time only)
# ──────────────────────────────────────────────────────────────

log "Bootstrapping CDK..."
cd "$INFRA_DIR"
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_DEFAULT_REGION="$AWS_REGION" \
cdk bootstrap \
    -c account="$AWS_ACCOUNT_ID" \
    -c region="$AWS_REGION" \
    aws://"$AWS_ACCOUNT_ID"/"$AWS_REGION"
ok "CDK bootstrapped"

# ──────────────────────────────────────────────────────────────
# 7. Deploy the CDK stack
# ──────────────────────────────────────────────────────────────

# Auto-delete stack if it's stuck in ROLLBACK_COMPLETE
STACK_STATUS=$(AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    AWS_DEFAULT_REGION="$AWS_REGION" \
    aws cloudformation describe-stacks \
        --stack-name NexusCloud \
        --region "$AWS_REGION" \
        --query 'Stacks[0].StackStatus' \
        --output text 2>/dev/null || echo "DOES_NOT_EXIST")
if [ "$STACK_STATUS" = "ROLLBACK_COMPLETE" ]; then
    warn "Stack NexusCloud is in ROLLBACK_COMPLETE — deleting before redeploy..."
    AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    AWS_DEFAULT_REGION="$AWS_REGION" \
    aws cloudformation delete-stack --stack-name NexusCloud --region "$AWS_REGION"
    AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
    AWS_DEFAULT_REGION="$AWS_REGION" \
    aws cloudformation wait stack-delete-complete --stack-name NexusCloud --region "$AWS_REGION"
    ok "Stack deleted — redeploying cleanly"
fi

log "Deploying NexusCloud stack..."
cd "$INFRA_DIR"
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_DEFAULT_REGION="$AWS_REGION" \
cdk deploy NexusCloud \
    -c account="$AWS_ACCOUNT_ID" \
    -c region="$AWS_REGION" \
    -c assets_bucket="${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}" \
    -c outputs_bucket="${OUTPUTS_BUCKET:-nexus-outputs}" \
    -c config_bucket="${CONFIG_BUCKET:-nexus-config-$AWS_ACCOUNT_ID}" \
    --require-approval never \
    --outputs-file "$PROJECT_DIR/cdk-outputs.json"
ok "CDK stack deployed!"

# ──────────────────────────────────────────────────────────────
# 8. Extract outputs
# ──────────────────────────────────────────────────────────────

if [ -f "$PROJECT_DIR/cdk-outputs.json" ]; then
    API_URL=$(python3 -c "
import json
with open('$PROJECT_DIR/cdk-outputs.json') as f:
    data = json.load(f)
stack = data.get('NexusCloud', {})
print(stack.get('ApiUrl', 'N/A'))
")
    DASHBOARD_URL=$(python3 -c "
import json
with open('$PROJECT_DIR/cdk-outputs.json') as f:
    data = json.load(f)
stack = data.get('NexusCloud', {})
print(stack.get('DashboardUrl', 'N/A'))
")
    STATE_MACHINE_ARN=$(python3 -c "
import json
with open('$PROJECT_DIR/cdk-outputs.json') as f:
    data = json.load(f)
stack = data.get('NexusCloud', {})
print(stack.get('StateMachineArn', 'N/A'))
")
    CONFIG_BUCKET=$(python3 -c "
import json
with open('$PROJECT_DIR/cdk-outputs.json') as f:
    data = json.load(f)
stack = data.get('NexusCloud', {})
print(stack.get('ConfigBucket', 'nexus-config'))
")
    DASHBOARD_BUCKET="nexus-dashboard-$AWS_ACCOUNT_ID"
else
    warn "No cdk-outputs.json found — extract values manually from CloudFormation"
    API_URL="N/A"
    DASHBOARD_URL="N/A"
    STATE_MACHINE_ARN="N/A"
    CONFIG_BUCKET="nexus-config"
    DASHBOARD_BUCKET="nexus-dashboard-$AWS_ACCOUNT_ID"
fi

# ──────────────────────────────────────────────────────────────
# 9. Upload profiles to config bucket
# ──────────────────────────────────────────────────────────────

log "Uploading profiles to S3..."
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_DEFAULT_REGION="$AWS_REGION" \
aws s3 cp "$PROJECT_DIR/profiles/" "s3://$CONFIG_BUCKET/" --recursive --content-type "application/json" --region "$AWS_REGION"
ok "Profiles uploaded to s3://$CONFIG_BUCKET/"

# ──────────────────────────────────────────────────────────────
# 9b. Generate & upload LUT files
# ──────────────────────────────────────────────────────────────

log "Generating and uploading LUT files..."
if python3 "$PROJECT_DIR/scripts/setup_luts.py" \
    --upload-to-s3 \
    --bucket "${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}"; then
    ok "LUTs uploaded to s3://${ASSETS_BUCKET}/luts/"
else
    warn "setup_luts.py failed — color grading will use fallback palette"
fi

# ──────────────────────────────────────────────────────────────
# 9c. Download & upload SFX files
# ──────────────────────────────────────────────────────────────

log "Downloading and uploading SFX files..."
if FREESOUND_API_KEY="${FREESOUND_API_KEY}" \
   python3 "$PROJECT_DIR/scripts/upload_sfx.py" \
    --bucket "${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}"; then
    ok "SFX uploaded to s3://${ASSETS_BUCKET}/sfx/"
else
    warn "upload_sfx.py failed — sound effects will be skipped at runtime"
fi

# ──────────────────────────────────────────────────────────────
# 10. Upload dashboard with API URL injected
# ──────────────────────────────────────────────────────────────

log "Uploading dashboard..."
# Replace placeholder API URL in dashboard
DASHBOARD_TMP=$(mktemp)
# Remove trailing slash from API_URL if present for clean URLs
API_URL_CLEAN="${API_URL%/}"
sed "s|%%NEXUS_API_BASE%%|${API_URL_CLEAN}|g" "$PROJECT_DIR/dashboard/index.html" > "$DASHBOARD_TMP"
AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
AWS_DEFAULT_REGION="$AWS_REGION" \
aws s3 cp "$DASHBOARD_TMP" "s3://$DASHBOARD_BUCKET/index.html" --content-type "text/html" --region "$AWS_REGION"
rm -f "$DASHBOARD_TMP"
ok "Dashboard uploaded to s3://$DASHBOARD_BUCKET/"

# ──────────────────────────────────────────────────────────────
# 11. Update .env with deployed values
# ──────────────────────────────────────────────────────────────

log "Updating .env with deployment outputs..."
sed -i.bak "s|^STATE_MACHINE_ARN=.*|STATE_MACHINE_ARN=$STATE_MACHINE_ARN|" "$PROJECT_DIR/.env"
sed -i.bak "s|^AWS_ACCOUNT_ID=.*|AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID|" "$PROJECT_DIR/.env"
sed -i.bak "s|^ASSETS_BUCKET=.*|ASSETS_BUCKET=${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}|" "$PROJECT_DIR/.env"
rm -f "$PROJECT_DIR/.env.bak"
ok ".env updated"

# ──────────────────────────────────────────────────────────────
# Done!
# ──────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          🚀  Nexus Cloud deployed successfully!             ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC} API URL:       ${CYAN}$API_URL${NC}"
echo -e "${GREEN}║${NC} Dashboard:     ${CYAN}$DASHBOARD_URL${NC}"
echo -e "${GREEN}║${NC} State Machine: ${CYAN}$STATE_MACHINE_ARN${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}                                                              "
echo -e "${GREEN}║${NC} Test with:                                                   "
echo -e "${GREEN}║${NC}   curl -X POST ${API_URL}run \\                              "
echo -e "${GREEN}║${NC}     -H 'Content-Type: application/json' \\                   "
echo -e "${GREEN}║${NC}     -d '{\"niche\":\"technology\",\"profile\":\"documentary\",\"dry_run\":true}'"
echo -e "${GREEN}║${NC}                                                              "
echo -e "${GREEN}║${NC} Or via AWS CLI:                                              "
echo -e "${GREEN}║${NC}   aws stepfunctions start-execution \\                       "
echo -e "${GREEN}║${NC}     --state-machine-arn $STATE_MACHINE_ARN \\                "
echo -e "${GREEN}║${NC}     --input '{\"niche\":\"technology\",\"profile\":\"documentary\",\"dry_run\":true}'"
echo -e "${GREEN}║${NC}                                                              "
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC} ${YELLOW}⚠ REMINDER: Enable Bedrock models in AWS Console:${NC}        "
echo -e "${GREEN}║${NC}   • us.anthropic.claude-3-sonnet-20240229-v1:0               "
echo -e "${GREEN}║${NC}   • us.anthropic.claude-3-5-sonnet-20241022-v2:0             "
echo -e "${GREEN}║${NC}   Region: $AWS_REGION                                        "
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ──────────────────────────────────────────────────────────────
# Cleanup helper
# ──────────────────────────────────────────────────────────────
echo -e "${YELLOW}To tear down all resources later:${NC}"
echo "  cd $INFRA_DIR && cdk destroy -c account=$AWS_ACCOUNT_ID -c region=$AWS_REGION"
echo ""



