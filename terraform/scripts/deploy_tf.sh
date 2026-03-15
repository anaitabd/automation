#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════╗
# ║  Nexus Cloud — Terraform Deployment Script                  ║
# ║  Replaces deploy.sh (CDK/Docker) with Terraform-native flow║
# ╚══════════════════════════════════════════════════════════════╝

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

TF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_DIR="$(cd "$TF_DIR/.." && pwd)"
BUILD_DIR="$TF_DIR/.build"

log()  { echo -e "${CYAN}[deploy-tf]${NC} $*"; }
ok()   { echo -e "${GREEN}[  ✅  ]${NC} $*"; }
warn() { echo -e "${YELLOW}[  ⚠️  ]${NC} $*"; }
err()  { echo -e "${RED}[  ❌  ]${NC} $*"; }

# ──────────────────────────────────────────────────────────────
# 0. Load .env & pre-flight checks
# ──────────────────────────────────────────────────────────────

log "Loading .env file..."
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/env.exemple" "$PROJECT_DIR/.env"
    warn ".env created from template — fill in values and re-run!"
    exit 1
fi

set -a
while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" ]] && continue
    eval "$line" 2>/dev/null || true
done < "$PROJECT_DIR/.env"
set +a
ok ".env loaded"

log "Running pre-flight checks..."
command -v terraform >/dev/null 2>&1 || { err "Terraform not found. Install: brew install terraform"; exit 1; }
command -v aws       >/dev/null 2>&1 || { err "AWS CLI not found. Install: brew install awscli"; exit 1; }
command -v docker    >/dev/null 2>&1 || { err "Docker not found. Install Docker Desktop"; exit 1; }
command -v python3   >/dev/null 2>&1 || { err "python3 not found"; exit 1; }

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    err "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in .env"
    exit 1
fi

export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
    err "AWS credentials invalid. Check .env"
    exit 1
fi

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ok "AWS Account: $AWS_ACCOUNT_ID | Region: $AWS_REGION"

# ──────────────────────────────────────────────────────────────
# 1. Copy shared pipeline utils into each Lambda
# ──────────────────────────────────────────────────────────────

SHARED_UTILS="$PROJECT_DIR/lambdas/nexus_pipeline_utils.py"
log "Copying shared pipeline utils into each Lambda..."
for lambda_dir in "$PROJECT_DIR"/lambdas/nexus-*/; do
    cp -f "$SHARED_UTILS" "$lambda_dir/nexus_pipeline_utils.py"
done
ok "Shared utils copied"

# ──────────────────────────────────────────────────────────────
# 2. Build Lambda layers (native packaging, no Docker needed)
# ──────────────────────────────────────────────────────────────

log "Building Lambda layers..."
mkdir -p "$BUILD_DIR/layers"

# --- API Layer ---
if [ ! -f "$BUILD_DIR/layers/api.zip" ] || [ "${FORCE_REBUILD:-}" = "1" ]; then
    log "Building API layer (pip install + zip)..."
    LAYER_TMP=$(mktemp -d)
    mkdir -p "$LAYER_TMP/python"
    # Use Docker for arm64 cross-compilation (same as old deploy.sh)
    docker run --rm --platform linux/arm64 \
        -v "$LAYER_TMP/python:/out" \
        python:3.12-slim \
        bash -c "pip install --no-cache-dir requests boto3 psycopg2-binary python-dotenv json-repair Pillow -t /out && rm -rf /out/*.dist-info /out/__pycache__"
    (cd "$LAYER_TMP" && zip -r9 "$BUILD_DIR/layers/api.zip" python/)
    rm -rf "$LAYER_TMP"
    ok "API layer built"
else
    ok "API layer already built (use FORCE_REBUILD=1 to rebuild)"
fi

# --- FFmpeg Layer ---
if [ ! -f "$BUILD_DIR/layers/ffmpeg.zip" ] || [ "${FORCE_REBUILD:-}" = "1" ]; then
    log "Building FFmpeg layer (static arm64 binaries)..."
    FFMPEG_TMP=$(mktemp -d)
    mkdir -p "$FFMPEG_TMP/bin"
    docker run --rm --platform linux/arm64 \
        -v "$FFMPEG_TMP/bin:/out" \
        debian:bookworm-slim \
        bash -c "apt-get update && apt-get install -y --no-install-recommends curl xz-utils ca-certificates && \
                 curl -L --retry 5 --retry-delay 10 --retry-all-errors \
                   -o /tmp/ffmpeg.tar.xz \
                   https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz && \
                 tar -xJf /tmp/ffmpeg.tar.xz --strip-components=1 -C /out --wildcards '*/ffmpeg' '*/ffprobe' && \
                 chmod +x /out/ffmpeg /out/ffprobe"
    (cd "$FFMPEG_TMP" && zip -r9 "$BUILD_DIR/layers/ffmpeg.zip" bin/)
    rm -rf "$FFMPEG_TMP"
    ok "FFmpeg layer built"
else
    ok "FFmpeg layer already built"
fi

# ──────────────────────────────────────────────────────────────
# 3. Build & push ECS container images
# ──────────────────────────────────────────────────────────────

log "Ensuring ECR repositories exist..."
for REPO in nexus-audio nexus-visuals nexus-editor nexus-shorts; do
    aws ecr describe-repositories --repository-names "$REPO" --region "$AWS_REGION" >/dev/null 2>&1 || \
        aws ecr create-repository --repository-name "$REPO" --region "$AWS_REGION" >/dev/null
done

ECR_URL="$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
log "Logging into ECR..."
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_URL"

for SERVICE in nexus-audio nexus-visuals nexus-editor nexus-shorts; do
    DOCKERFILE="$PROJECT_DIR/lambdas/$SERVICE/Dockerfile"
    if [ ! -f "$DOCKERFILE" ]; then
        warn "No Dockerfile for $SERVICE — skipping"
        continue
    fi
    log "Building $SERVICE container image (arm64)..."
    docker build --platform linux/arm64 \
        -f "$DOCKERFILE" \
        -t "$ECR_URL/$SERVICE:latest" \
        "$PROJECT_DIR"
    log "Pushing $SERVICE to ECR..."
    docker push "$ECR_URL/$SERVICE:latest"
    ok "$SERVICE image pushed to ECR"
done

# ──────────────────────────────────────────────────────────────
# 4. Generate terraform.tfvars from .env
# ──────────────────────────────────────────────────────────────

log "Generating terraform.tfvars from .env..."
cat > "$TF_DIR/terraform.tfvars" << EOF
aws_region          = "${AWS_REGION}"
environment         = "prod"
project_root        = "${PROJECT_DIR}"

assets_bucket_name  = "${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}"
outputs_bucket_name = "${OUTPUTS_BUCKET:-nexus-outputs-$AWS_ACCOUNT_ID}"
config_bucket_name  = "${CONFIG_BUCKET:-nexus-config-$AWS_ACCOUNT_ID}"

perplexity_api_key  = "${PERPLEXITY_API_KEY:-}"
elevenlabs_api_key  = "${ELEVENLABS_API_KEY:-}"
pexels_api_key      = "${PEXELS_API_KEY:-}"
pixabay_api_key     = "${PIXABAY_API_KEY:-}"
freesound_api_key   = "${FREESOUND_API_KEY:-}"
discord_webhook_url = "${DISCORD_WEBHOOK_URL:-}"

youtube_client_id     = "${YOUTUBE_CLIENT_ID:-}"
youtube_client_secret = "${YOUTUBE_CLIENT_SECRET:-}"
youtube_refresh_token = "${YOUTUBE_REFRESH_TOKEN:-}"

db_host     = "${DB_HOST:-postgres}"
db_port     = "${DB_PORT:-5432}"
db_name     = "${DB_NAME:-nexus}"
db_user     = "${DB_USER:-nexus_user}"
db_password = "${DB_PASSWORD:-changeme}"
EOF
ok "terraform.tfvars generated"

# ──────────────────────────────────────────────────────────────
# 5. Terraform init + plan + apply
# ──────────────────────────────────────────────────────────────

cd "$TF_DIR"

log "terraform init..."
terraform init -input=false
ok "Terraform initialized"

log "terraform plan..."
terraform plan -input=false -out=tfplan
ok "Plan generated"

echo ""
read -rp "$(echo -e "${YELLOW}Apply this plan? [y/N]:${NC} ")" APPLY
if [[ ! "$APPLY" =~ ^[Yy]$ ]]; then
    err "Aborted. Plan saved to terraform/tfplan"
    exit 1
fi

log "terraform apply..."
terraform apply -input=false tfplan
ok "Terraform applied!"

# ──────────────────────────────────────────────────────────────
# 6. Extract outputs & upload dashboard
# ──────────────────────────────────────────────────────────────

API_URL=$(terraform output -raw api_url 2>/dev/null || echo "N/A")
DASHBOARD_URL=$(terraform output -raw dashboard_url 2>/dev/null || echo "N/A")
STATE_MACHINE_ARN=$(terraform output -raw state_machine_arn 2>/dev/null || echo "N/A")
DASHBOARD_BUCKET="nexus-dashboard-$AWS_ACCOUNT_ID"

# Upload dashboard with API URL injected
log "Uploading dashboard..."
DASHBOARD_TMP=$(mktemp)
API_URL_CLEAN="${API_URL%/}"
sed "s|%%NEXUS_API_BASE%%|${API_URL_CLEAN}|g" "$PROJECT_DIR/dashboard/index.html" > "$DASHBOARD_TMP"
aws s3 cp "$DASHBOARD_TMP" "s3://$DASHBOARD_BUCKET/index.html" \
    --content-type "text/html" --region "$AWS_REGION"
rm -f "$DASHBOARD_TMP"
ok "Dashboard uploaded"

# Generate & upload LUTs
log "Generating and uploading LUT files..."
python3 "$PROJECT_DIR/scripts/setup_luts.py" \
    --upload-to-s3 \
    --bucket "${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}" 2>/dev/null && \
    ok "LUTs uploaded" || warn "setup_luts.py failed — color grading will use fallback"

# Upload SFX
log "Uploading SFX files..."
FREESOUND_API_KEY="${FREESOUND_API_KEY:-}" \
python3 "$PROJECT_DIR/scripts/upload_sfx.py" \
    --bucket "${ASSETS_BUCKET:-nexus-assets-$AWS_ACCOUNT_ID}" 2>/dev/null && \
    ok "SFX uploaded" || warn "upload_sfx.py failed — sound effects will be skipped"

# ──────────────────────────────────────────────────────────────
# 7. Update .env with deployed values
# ──────────────────────────────────────────────────────────────

log "Updating .env with deployment outputs..."
sed -i.bak "s|^STATE_MACHINE_ARN=.*|STATE_MACHINE_ARN=$STATE_MACHINE_ARN|" "$PROJECT_DIR/.env"
sed -i.bak "s|^AWS_ACCOUNT_ID=.*|AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID|" "$PROJECT_DIR/.env"
rm -f "$PROJECT_DIR/.env.bak"
ok ".env updated"

# ──────────────────────────────────────────────────────────────
# Done!
# ──────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          🚀  Nexus Cloud deployed via Terraform!            ║${NC}"
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
echo -e "${GREEN}║${NC} Terraform ops:                                               "
echo -e "${GREEN}║${NC}   cd terraform && terraform plan                             "
echo -e "${GREEN}║${NC}   cd terraform && terraform apply                            "
echo -e "${GREEN}║${NC}   cd terraform && terraform destroy                          "
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC} ${YELLOW}⚠ REMINDER: Enable Bedrock models in AWS Console:${NC}        "
echo -e "${GREEN}║${NC}   • anthropic.claude-sonnet-4-5-20250929-v1:0               "
echo -e "${GREEN}║${NC}   • anthropic.claude-opus-4-5-20251101-v1:0                 "
echo -e "${GREEN}║${NC}   Region: $AWS_REGION                                        "
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

