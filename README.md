# Nexus Cloud — Serverless YouTube Automation Pipeline

> Turn any niche keyword into a fully produced, uploaded YouTube video — entirely
> on AWS, entirely serverless. One API call to run. One command to deploy (Terraform).

---

## Architecture

```
                     ┌─────────────┐      ┌─────────────────────┐
  Browser/API ──────▶│ API Gateway │      │ CloudFront Dashboard │
                     └──────┬──────┘      └──────────────────────┘
                            │ POST /run  (x-api-key required)
                     ┌──────▼──────┐
                     │    Step     │
                     │  Functions  │
                     └──────┬──────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
      Research ──▶ Script ──▶ ┌─AudioVisuals──┐
                              │  Audio (ECS)  │  ← parallel
                              │  Visuals (ECS)│
                              └──────┬────────┘
                                     │ MergeParallelOutputs
                              ┌──────▼────────────────────┐
                              │  ContentAssembly (parallel)│
                              │  ┌─────────┐ ┌──────────┐ │
                              │  │ Editor  │ │  Shorts  │ │  ← parallel
                              │  │  (ECS)  │ │  (ECS)   │ │
                              │  └─────────┘ └──────────┘ │
                              └──────┬────────────────────┘
                                     │ MergeContentOutputs
                              Thumbnail ──▶ Upload ──▶ Notify

                    S3 (3 buckets): assets · outputs · config
                    EFS: /mnt/scratch  (shared ECS scratch space)
```

### Pipeline Steps

| # | Step | Runtime | What it does |
|---|------|---------|-------------|
| 1 | `nexus-research` | Lambda | Perplexity `sonar-pro` trend research + Bedrock Claude topic selection |
| 2 | `nexus-script` | Lambda | 6-pass script generation — structure → fact-integrity → hook-rewrite → visual-cues → pacing → Perplexity fact-check |
| 3 | `nexus-audio` | ECS Fargate | ElevenLabs TTS (sentence-level, emotion-aware) + Pixabay/Freesound music + FFmpeg mixing |
| 4 | `nexus-visuals` | ECS Fargate | Nova Reel + Nova Canvas b-roll generation + Pexels/Pixabay stock fallback + CLIP scoring |
| 5 | `nexus-editor` | ECS Fargate | Beat-synced assembly, LUT colour-grading, overlays + AWS MediaConvert transcode → long-form MP4 |
| 5b | `nexus-shorts` | ECS Fargate | 15s/30s/45s/60s vertical short-form MP4s in parallel with Editor (optional, `generate_shorts: true`) |
| 6 | `nexus-thumbnail` | Lambda | NVIDIA NIM vision frame-scoring → 3 thumbnail composites → S3 |
| 7 | `nexus-upload` | Lambda | YouTube Data API v3 OAuth2 upload (manual approval by default) |
| 8 | `nexus-notify` | Lambda | Discord embed notification + PostgreSQL run log |

### Pipeline Modes (Dashboard)

| Mode | Description | Trigger |
|------|-------------|---------|
| `video` | Long-form only — Research → Script → AudioVisuals → Editor → Thumbnail → Notify | `pipeline_type: "video"` |
| `shorts` | Short-form only — Research → Script → AudioVisuals → Shorts → Notify | `pipeline_type: "shorts"` |
| `combined` | Long-form + Shorts in parallel (legacy mode) | `generate_shorts: true` |

---

## Project Structure

```
automation/
├── deploy.sh                          ← Legacy CDK deploy (frozen, use Terraform instead)
├── docker-compose.yml                 ← Local dev stack (Postgres + all services)
├── Dockerfile / Dockerfile.setup      ← Standard Lambda image / AWS bootstrap
├── requirements.txt                   ← Python deps (local dev / tests)
├── pytest.ini                         ← Test config + markers
├── env.exemple                        ← .env template
│
├── terraform/                         ← PRIMARY DEPLOY PATH
│   ├── main.tf                        ← Root module wiring
│   ├── variables.tf / outputs.tf
│   ├── modules/
│   │   ├── storage/                   ← S3, dashboard bucket, profile uploads
│   │   ├── secrets/                   ← Secrets Manager (all nexus/* secrets)
│   │   ├── networking/                ← VPC, EFS, NFS security group
│   │   ├── identity/                  ← IAM roles (Lambda, ECS, SFN, API, MediaConvert)
│   │   ├── compute/                   ← Lambda zips, ECS cluster, ECR repos, Fargate task defs
│   │   ├── orchestration/             ← Step Functions state machine via templatefile()
│   │   ├── api/                       ← API Gateway + CloudFront distribution
│   │   └── observability/             ← EventBridge schedule, CloudWatch dashboard
│   └── scripts/
│       ├── deploy_tf.sh               ← ✅ ONE-COMMAND DEPLOY (builds layers + ECS images)
│       └── validate_deploy.sh         ← Post-deploy sanity check
│
├── lambdas/
│   ├── nexus_pipeline_utils.py        ← Shared utils (auto-copied by deploy_tf.sh)
│   ├── nexus-api/
│   │   ├── handler.py                 ← REST API handler (/run /resume /status /outputs /health)
│   │   ├── preflight.py               ← 9-service pre-flight health checks (parallel)
│   │   └── db.py                      ← PostgreSQL channel CRUD (connection pool)
│   ├── nexus-research/handler.py
│   ├── nexus-script/handler.py
│   ├── nexus-audio/                   ← ECS Fargate (Dockerfile)
│   ├── nexus-visuals/                 ← ECS Fargate (Dockerfile)
│   ├── nexus-editor/                  ← ECS Fargate (Dockerfile + TypeScript render.js)
│   ├── nexus-shorts/                  ← ECS Fargate — 15 modules (Dockerfile)
│   │   ├── handler.py / config.py / batch_processor.py
│   │   ├── section_scorer.py / script_condenser.py / voiceover_generator.py
│   │   ├── broll_fetcher.py / vertical_converter.py / motion_renderer.py
│   │   ├── beat_syncer.py / clip_assembler.py / loop_builder.py
│   │   └── audio_mixer.py / color_grader.py / watermarker.py / uploader.py
│   ├── nexus-thumbnail/handler.py
│   ├── nexus-upload/handler.py
│   ├── nexus-notify/handler.py
│   ├── nexus-channel-setup/handler.py ← Orchestrates brand → logo → intro/outro
│   ├── nexus-brand-designer/handler.py← Claude brand kit generation
│   ├── nexus-logo-gen/handler.py      ← Nova Canvas logo + Pillow fallback
│   └── shared/                        ← nova_canvas.py · nova_reel.py · motion.py
│
├── statemachine/
│   └── nexus_pipeline.asl.json        ← ASL with parallel AudioVisuals + ContentAssembly
│
├── profiles/
│   ├── documentary.json               ← 10–16 min, dissolve, cinematic warm
│   ├── finance.json                   ← 8–14 min, cut, clean corporate
│   └── entertainment.json             ← 6–12 min, zoom punch, punchy vibrant
│
├── dashboard/
│   └── index.html                     ← Single-file React 18 monitoring dashboard
│
├── scripts/
│   ├── orchestrator.py                ← Local Step-Functions-like runner (serves dashboard :3000)
│   ├── setup_aws.py / setup_luts.py / upload_sfx.py
│   ├── approve_upload.py / resume_run.py / check_external.py
│   └── tests/                         ← pytest test suite (132+ tests)
│       ├── conftest.py                ← Shared fixtures + handler loader
│       ├── test_api_handler.py        ← API routing, validation, SFN forwarding
│       ├── test_research_handler.py   ← Perplexity + Bedrock topic selection
│       ├── test_script_handler.py     ← 6-pass generation + JSON repair
│       ├── test_preflight.py          ← 9-service health checks
│       ├── test_notify_handler.py     ← Discord + PostgreSQL logging
│       ├── test_pipeline_utils.py     ← Shared utilities
│       ├── test_shorts_config.py      ← Tier defs, resolution, BPM, LUTs
│       ├── test_shorts_section_scorer.py ← Section scoring algorithm
│       ├── test_brand_designer_handler.py
│       └── test_channel_setup_handler.py
│
└── infrastructure/                    ← Legacy CDK (frozen — do not modify)
    └── nexus_stack.py
```

---

## Prerequisites

| Tool | Min version | Install |
|------|-------------|---------|
| **AWS CLI** | 2.x | `brew install awscli` |
| **Terraform** | 1.6+ | `brew install terraform` |
| **Python** | 3.12+ | `brew install python@3.12` |
| **Docker** | 24+ | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| **Node.js** | 20+ | `brew install node` (for Editor Lambda TypeScript build) |

### AWS Account Requirements

- IAM user with admin permissions (or scoped: S3, Lambda, IAM, Secrets Manager, Step Functions, API Gateway, CloudFront, EventBridge, CloudWatch, Bedrock, MediaConvert, ECR, ECS, EFS)
- **Bedrock models enabled** in your region (`us-east-1`):
  - `anthropic.claude-3-sonnet-20240229-v1:0`
  - `anthropic.claude-3-5-sonnet-20241022-v2:0`
  - `amazon.nova-canvas-v1:0`
  - `amazon.nova-reel-v1:0`

---

## Quick Start — Full AWS Deployment (Terraform)

### 1. Clone & configure

```bash
git clone <repo-url> && cd automation
cp env.exemple .env
```

Edit `.env` with **at minimum**:

```dotenv
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_ACCOUNT_ID=123456789012
AWS_REGION=us-east-1

PERPLEXITY_API_KEY=pplx-...
ELEVENLABS_API_KEY=sk_...
PEXELS_API_KEY=...
PIXABAY_API_KEY=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DB_PASSWORD=your_secure_password
NEXUS_API_KEY=your-secret-api-key      # x-api-key for all /run /status etc.
```

### 2. Deploy everything

```bash
bash terraform/scripts/deploy_tf.sh
```

This single command:

1. Reads `.env` and validates credentials
2. Copies shared utils into every Lambda package
3. Builds Lambda layers (arm64 cross-compiled via Docker)
4. Builds and pushes all ECS Docker images to ECR
5. Generates `terraform/terraform.tfvars`
6. Runs `terraform apply` (8 modules: storage → secrets → networking → identity → compute → orchestration → api → observability)

At the end you'll see:

```
✅  Nexus Cloud deployed successfully via Terraform!
  API URL:       https://xxxxxxxx.execute-api.us-east-1.amazonaws.com/prod/
  Dashboard:     https://dxxxxxxxxx.cloudfront.net
  State Machine: arn:aws:states:us-east-1:XXXX:stateMachine:nexus-pipeline
```

### 3. Validate the deployment

```bash
cd terraform && bash scripts/validate_deploy.sh
```

Checks API health, SFN state machine, S3 buckets, all Lambda functions, ECS cluster, and performs a dry-run execution.

### 4. Trigger a run

```bash
# Health check (no API key required)
curl https://<your-api-url>/health

# Dry run — no AI calls, validates plumbing
curl -X POST https://<your-api-url>/run \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your-api-key' \
  -d '{"niche":"technology","profile":"documentary","dry_run":true}'

# Full long-form video run
curl -X POST https://<your-api-url>/run \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your-api-key' \
  -d '{"niche":"obscure history","profile":"documentary","generate_shorts":false}'

# Full run with Shorts (generates 15s/30s/45s/60s verticals in parallel)
curl -X POST https://<your-api-url>/run \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your-api-key' \
  -d '{
    "niche": "quantum computing",
    "profile": "documentary",
    "generate_shorts": true,
    "shorts_tiers": "micro,short,mid,full",
    "channel_id": "ch-your-channel-id"
  }'
```

### 5. Check status & outputs

```bash
# Status (includes current step, progress %, ETA)
curl -H 'x-api-key: your-api-key' \
  https://<your-api-url>/status/<run_id>

# Presigned URLs for all outputs (video, thumbnails, script, shorts)
curl -H 'x-api-key: your-api-key' \
  https://<your-api-url>/outputs/<run_id>

# Resume a failed run (auto-detects last completed step from S3)
curl -X POST https://<your-api-url>/resume \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your-api-key' \
  -d '{"run_id":"<run_id>"}'
```

---

## Local Development with Docker

> Full guide: [dockeruse.md](dockeruse.md)

```bash
# Start everything (Postgres + AWS bootstrap + all services)
docker compose up --build

# Run the local orchestrator (Step-Functions-like) + dashboard on :3000
python3 scripts/orchestrator.py

# Invoke research Lambda locally
curl -s -X POST http://localhost:9001/2015-03-31/functions/function/invocations \
  -d '{"niche":"obscure history","profile":"documentary","dry_run":true}'

# Stop & clean up
docker compose down -v
```

### Local Service Ports

| Service | Port | Notes |
|---------|------|-------|
| Research | 9001 | `nexus-research` |
| Script | 9002 | `nexus-script` |
| Audio | 9003 | `nexus-audio` (ECS) |
| Visuals | 9004 | `nexus-visuals` (ECS) |
| Editor | 9005 | `nexus-editor` (ECS) |
| Thumbnail | 9006 | `nexus-thumbnail` |
| Upload | 9007 | `nexus-upload` |
| Notify | 9008 | `nexus-notify` |
| API | 9009 | `nexus-api` |
| Shorts | 9014 | `nexus-shorts` (ECS, 8 GB / 4 vCPU) |
| Dashboard | 3000 | Served by `orchestrator.py` |

---

## Running Tests

```bash
pip install -r requirements.txt pytest-mock vcrpy

# All tests (132+ collected)
python3 -m pytest scripts/tests/ -v

# By layer (markers)
python3 -m pytest scripts/tests/ -m unit -v --tb=short
python3 -m pytest scripts/tests/ -m regression -v --tb=short
python3 -m pytest scripts/tests/ -m security -v --tb=short
python3 -m pytest scripts/tests/ -m performance -v --tb=short
python3 -m pytest scripts/tests/ -m cost -v --tb=short

# Post-deploy smoke tests (requires NEXUS_API_URL env var)
NEXUS_API_URL=https://xxx.execute-api.us-east-1.amazonaws.com \
NEXUS_API_KEY=your-key \
python3 -m pytest scripts/tests/ -m smoke -v --tb=short
```

### Test Suite Overview (9 Layers)

| Layer | Marker | Files | Status |
|-------|--------|-------|--------|
| 1 — Unit | `unit` | `test_api_handler`, `test_research_handler`, `test_script_handler`, `test_preflight`, `test_notify_handler`, `test_pipeline_utils`, `test_shorts_*`, `test_brand_designer`, `test_channel_setup` | ✅ 132 tests |
| 2 — Contract | `contract` | `test_contracts` (vcrpy cassettes) | 🔜 Planned |
| 3 — Regression | `regression` | `test_regression` | 🔜 Planned |
| 4 — Security | `security` | `test_security` | 🔜 Planned |
| 5 — Validation | `unit` | `test_output_validator` | 🔜 Planned |
| 6 — Performance | `performance` | `test_performance` | 🔜 Planned |
| 7 — Smoke | `smoke` | `test_smoke` | 🔜 Planned |
| 8 — Chaos | `chaos` | `test_chaos` | 🔜 Planned |
| 9 — Cost | `cost` | `test_cost_regression` | 🔜 Planned |

---

## Required Secrets (AWS Secrets Manager)

All secrets are managed by Terraform (`terraform/modules/secrets/`). They are **never read from environment variables at Lambda runtime** — all are fetched from Secrets Manager at cold-start and cached in-memory (`_cache` dict in each handler).

| Secret Name | JSON Key(s) | Source |
|-------------|------------|--------|
| `nexus/perplexity_api_key` | `api_key` | [Perplexity](https://www.perplexity.ai/settings/api) |
| `nexus/elevenlabs_api_key` | `api_key` | [ElevenLabs](https://elevenlabs.io) |
| `nexus/pexels_api_key` | `api_key` | [Pexels](https://www.pexels.com/api/) |
| `nexus/pixabay_api_key` | `api_key` | [Pixabay](https://pixabay.com/api/docs/) |
| `nexus/freesound_api_key` | `api_key` | [Freesound](https://freesound.org/apiv2/) |
| `nexus/youtube_credentials` | `client_id`, `client_secret`, `refresh_token`, `access_token` | Google Cloud Console OAuth2 |
| `nexus/discord_webhook_url` | `url` | Discord → Server Settings → Integrations → Webhooks |
| `nexus/db_credentials` | `host`, `port`, `dbname`, `user`, `password` | PostgreSQL / RDS |
| `nexus/api_key` | `key` | Your chosen auth key for the REST API (`x-api-key` header) |

---

## Channel Profiles

Stored in `profiles/` and uploaded to the `nexus-config` S3 bucket during Terraform deploy.

| Profile | Duration | Script Passes | Transition | Colour Grade | Music Mood |
|---------|----------|--------------|------------|-------------|-----------|
| `documentary` | 10–16 min | 6 (Perplexity fact-check) | dissolve | cinematic warm | tension / atmospheric |
| `finance` | 8–14 min | 6 (Perplexity fact-check) | cut | clean corporate | corporate upbeat |
| `entertainment` | 6–12 min | 6 (Perplexity fact-check) | zoom punch | punchy vibrant | energetic hype |

Profile keys used at runtime: `voice.voice_id`, `voice.stability`, `voice.similarity_boost`, `voice.style`, `llm.script_model`, `script.target_duration_min/max`, `editing.cuts_per_minute_target`, `visuals.color_grade_default`.

---

## nexus-shorts — Short-Form Video Module

When `generate_shorts: true` is set, a second ECS Fargate task runs in parallel with the Editor and produces:

| Tier | Duration | Nova Reel Clips | B-roll Strategy |
|------|----------|----------------|-----------------|
| `micro` | 15 s | 2 | Nova Reel → Pexels → Nova Canvas → gradient |
| `short` | 30 s | 4 | (same 4-tier fallback) |
| `mid` | 45 s | 5 | |
| `full` | 60 s | 6 | |

**Output specs**: MP4 H.264+AAC, 1080×1920 (9:16), 30 fps, CRF 18, −14 LUFS, seamless loop, faststart.

**S3 layout**: `{run_id}/shorts/short_{tier}_{n}.mp4` + `manifest.json`

---

## Channel Management

Channels store brand identity and per-channel voice/profile settings. Setup is orchestrated by `nexus-channel-setup` which calls:

1. `nexus-brand-designer` — Claude generates brand kit (colors, font, tone)
2. `nexus-logo-gen` — Nova Canvas generates logo (Pillow fallback)
3. `nexus-intro-outro` — Generates intro/outro clips (stub, non-fatal on failure)

**Channel CRUD API** (all require `x-api-key`):

```bash
POST   /channel/create          # Create channel
GET    /channel/list            # List active channels
GET    /channel/{id}            # Get channel details
PUT    /channel/{id}/brand      # Update brand kit
DELETE /channel/{id}            # Archive (soft delete)
GET    /channel/{id}/videos     # List videos for channel
```

---

## AI Models Used

| Task | Model | Provider |
|------|-------|----------|
| Topic research | `sonar-pro` | Perplexity |
| Script generation (6 passes) | `claude-3-5-sonnet-20241022-v2:0` | AWS Bedrock |
| Script fact-check (pass 6) | `sonar-pro` | Perplexity |
| B-roll image generation | `amazon.nova-canvas-v1:0` | AWS Bedrock |
| B-roll video generation | `amazon.nova-reel-v1:0` | AWS Bedrock |
| Brand kit generation | `claude-3-5-sonnet-20241022-v2:0` | AWS Bedrock |
| Thumbnail frame scoring | `microsoft/phi-3.5-vision-instruct` | NVIDIA NIM |
| Thumbnail concept generation | `meta/llama-3.1-70b-instruct` | NVIDIA NIM |
| Text-to-speech | `eleven_multilingual_v2` | ElevenLabs |
| Video transcoding (>10 min) | MediaConvert | AWS |

---

## Cost Estimate per Run

> Approximate for a 12-minute documentary video (long-form only, no Shorts).

| Service | Usage | Cost |
|---------|-------|------|
| Perplexity sonar-pro | ~2K tokens × 2 calls (research + fact-check) | ~$0.03 |
| Bedrock Claude 3.5 Sonnet | ~60K tokens (6-pass script + brand) | ~$0.90 |
| Amazon Nova Canvas | ~6 image generations | ~$0.06 |
| Amazon Nova Reel | ~6 video clips × 5s | ~$0.30 |
| ElevenLabs TTS | ~15K characters | ~$0.60 |
| Lambda compute | 9 functions, ~45 min total | ~$0.15 |
| ECS Fargate | Audio + Visuals + Editor (~40 min, 4 vCPU) | ~$0.20 |
| S3 storage + transfer | ~500 MB/run | ~$0.02 |
| MediaConvert | HD transcode | ~$0.05 |
| Step Functions | 9 state transitions + parallel | ~$0.00 |
| **Total (long-form)** | | **~$2.00–$2.50** |
| **+ Shorts (4 tiers)** | Additional Nova Reel + ElevenLabs × 4 | **+~$1.50** |

---

## YouTube Upload — Manual Approval

By default `YOUTUBE_AUTO_PUBLISH=false`. Videos are saved to S3 for review.

```bash
# Review and upload manually
python3 scripts/approve_upload.py <run_id>
```

To enable auto-publish, set in `.env`:

```dotenv
YOUTUBE_AUTO_PUBLISH=true
YOUTUBE_CLIENT_ID=your_client_id
YOUTUBE_CLIENT_SECRET=your_secret
YOUTUBE_REFRESH_TOKEN=your_token
```

---

## Resume a Failed Run

The pipeline stores artifacts to S3 after each step. If a run fails mid-way, you can resume from the last completed step without re-running earlier steps:

```bash
# Via API
curl -X POST https://<api-url>/resume \
  -H 'x-api-key: your-key' \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>"}'

# Via CLI
python3 scripts/resume_run.py <run_id>
```

The resume handler detects the furthest completed artifact in S3 (`research.json` → `script.json` → `audio/mixed_audio.wav` → …) and injects `resume_from` into the ASL `ResumeRouter` Choice state.

---

## Error Handling

- Every Lambda/Fargate task wraps logic in `try/except` → writes errors to `s3://nexus-outputs/{run_id}/errors/{step}.json`
- Step Functions `.catch` on each state routes failures to `nexus-notify` (error path) → Discord alert
- LLM calls retry up to 3× with exponential backoff (`2^attempt` seconds)
- ElevenLabs failures retry 3× (first retry strips pacing markers)
- Shorts individual-tier failures are non-fatal: `manifest.json` records per-short status, pipeline continues
- `nexus-intro-outro` failures are non-fatal in channel setup

---

## Scheduled Runs (EventBridge)

An EventBridge rule `nexus-pipeline-schedule` is created but **disabled** by default.

```bash
# Enable (runs twice daily at 09:00 / 21:00 UTC)
aws events enable-rule --name nexus-pipeline-schedule

# Disable
aws events disable-rule --name nexus-pipeline-schedule
```

---

## Scheduled Runs (EventBridge)

An EventBridge rule `nexus-pipeline-schedule` is created but **disabled** by default (twice daily at 09:00 / 21:00 UTC).

```bash
# Enable
aws events enable-rule --name nexus-pipeline-schedule

# Disable
aws events disable-rule --name nexus-pipeline-schedule
```

Edit the schedule target input in the AWS Console or CDK to set your niche/profile.

---

## CloudWatch Dashboard

A dashboard named `nexus-pipeline` is auto-created showing:
- Lambda p95 duration for all 8 functions
- Lambda error counts
- View in AWS Console → CloudWatch → Dashboards → `nexus-pipeline`

---

## Teardown

```bash
cd terraform
terraform destroy
```

To also remove resources not managed by Terraform:

```bash
# Empty and delete S3 buckets
for b in nexus-outputs nexus-assets nexus-config; do
  aws s3 rm "s3://$b" --recursive && aws s3 rb "s3://$b"
done

# Delete all Nexus secrets
aws secretsmanager list-secrets --query "SecretList[?starts_with(Name,'nexus/')].Name" \
  --output text | tr '\t' '\n' | while read s; do
  aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery
done
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `deploy_tf.sh` fails at credentials | Ensure `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ACCOUNT_ID` are in `.env` |
| Bedrock `AccessDeniedException` | Enable models in AWS Console → Bedrock → Model access (`us-east-1`) |
| ECS images missing on `terraform apply` | Run `deploy_tf.sh` fully — it pushes images before `apply` |
| Lambda layers missing | `deploy_tf.sh` builds them in `terraform/.build/layers/`; never run `terraform apply` directly |
| `ExecutionDoesNotExist` on `/status` | The SFN ARN is pre-computed — if you renamed the state machine, update `main.tf` |
| Shorts not running | Check `generate_shorts: true` is forwarded in `/run` body AND `nexus-intro-outro` stub doesn't block |
| MediaConvert role creation fails | IAM user needs `iam:CreateRole` + `iam:PutRolePolicy` permissions |
| YouTube upload fails | Set OAuth2 credentials in Secrets Manager: `nexus/youtube_credentials` |
| Docker build hangs on ffmpeg download | Check internet; the static binary is ~70 MB |
| ECS task OOM | Increase `cpu`/`memory` in `terraform/modules/compute/main.tf` task definition |
| `401` on API call | Add `x-api-key: your-key` header — all routes except `/health` require it |
| `403` on API call | API key present but wrong value — check `nexus/api_key` secret |

---

## Terraform Module Map

| Module | Manages |
|--------|---------|
| `storage` | S3 buckets import, dashboard bucket, profile uploads |
| `secrets` | All `nexus/*` Secrets Manager secrets |
| `networking` | Default VPC lookup, EFS file system + access point, NFS security group |
| `identity` | IAM roles (Lambda, ECS execution/task, MediaConvert, SFN, API) |
| `compute` | Lambda zips, ECS cluster, ECR repos, Fargate task definitions |
| `orchestration` | Step Functions state machine via `templatefile()` |
| `api` | API Gateway REST API (5 routes + CORS), CloudFront distribution |
| `observability` | EventBridge schedule (disabled), CloudWatch dashboard |

---

## License

Private — All rights reserved.
