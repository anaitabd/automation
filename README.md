# Nexus Cloud — Serverless YouTube Automation Pipeline

> Turn any niche keyword into a fully produced, uploaded YouTube video — entirely
> on AWS, entirely serverless. One command to deploy, one API call to run.

---

## Architecture

```
                          ┌──────────────┐
                          │ API Gateway  │
                          └──────┬───────┘
                                 │ POST /run
                          ┌──────▼───────┐
                          │    Step      │
                          │  Functions   │
                          └──────┬───────┘
                                 │
      ┌──────────┬──────────┬────┴────┬──────────┬──────────┬──────────┬──────────┐
      ▼          ▼          ▼         ▼          ▼          ▼          ▼          ▼
  Research → Script → Audio → Visuals → Editor → Thumbnail → Upload → Notify
  (Lambda)  (Lambda) (Fargate)(Fargate)(Fargate) (Lambda)  (Lambda) (Lambda)
      │          │          │         │          │          │          │          │
      └──────────┴──────────┴────┬────┴──────────┴──────────┴──────────┴──────────┘
                                 │
                    S3 (4 buckets): assets · outputs · config · dashboard
                    EFS: shared scratch volume for Fargate tasks
```

| Step | Runtime | What it does |
|------|---------|-------------|
| 1 | Lambda `nexus-research` | Perplexity sonar-pro + Bedrock Claude 3 Sonnet → best topic & angle |
| 2 | Lambda `nexus-script` | 5-pass script generation (Bedrock Claude 3 Sonnet + Perplexity fact-check) |
| 3 | **Fargate** `nexus-audio` | ElevenLabs TTS + ffmpeg audio EQ + Pixabay background music + SFX mixing |
| 4 | **Fargate** `nexus-visuals` | Pexels / Archive.org stock footage + CLIP semantic scoring |
| 5 | **Fargate** `nexus-editor` | Beat-synced video assembly + ffmpeg overlays + AWS MediaConvert transcode |
| 6 | Lambda `nexus-thumbnail` | Bedrock Vision frame scoring → Claude concept gen → ffmpeg composite render |
| 7 | Lambda `nexus-upload` | YouTube Data API v3 OAuth2 upload (manual approval by default) |
| 8 | Lambda `nexus-notify` | Discord webhook notification + PostgreSQL run logging |

> **Note:** Audio, Visuals, and Editor run as ECS Fargate tasks (ARM64, 4 vCPU / 16 GB) sharing an EFS scratch volume at `/mnt/scratch`. All other steps run as ARM64 Lambda functions.

---

## Project Structure

```
automation/
├── deploy.sh                          ← One-command full AWS deployment
├── docker-compose.yml                 ← Local dev stack (Postgres + all services)
├── Dockerfile                         ← Lambda container image (standard Lambdas)
├── Dockerfile.setup                   ← AWS bootstrap container
├── requirements.txt                   ← Python deps (local dev / tests)
├── pytest.ini                         ← Test configuration
├── env.exemple                        ← Template for .env
│
├── lambdas/
│   ├── nexus_pipeline_utils.py        ← Shared utilities (copied into each Lambda by deploy.sh)
│   ├── shared/                        ← Shared modules (imported by multiple Lambdas)
│   │   ├── nova_canvas.py             ← Amazon Nova Canvas image-generation client
│   │   ├── nova_reel.py               ← Amazon Nova Reel video-generation client
│   │   └── brand_kit.py               ← Brand kit helpers (colours, fonts, logos)
│   ├── nexus-research/handler.py
│   ├── nexus-script/handler.py
│   ├── nexus-audio/                   ← ECS Fargate task (has its own Dockerfile)
│   │   ├── Dockerfile
│   │   └── handler.py
│   ├── nexus-visuals/                 ← ECS Fargate task (has its own Dockerfile)
│   │   ├── Dockerfile
│   │   └── handler.py
│   ├── nexus-editor/                  ← ECS Fargate task (has its own Dockerfile)
│   │   ├── Dockerfile
│   │   └── handler.py
│   ├── nexus-thumbnail/handler.py
│   ├── nexus-upload/handler.py
│   ├── nexus-notify/handler.py
│   ├── nexus-api/handler.py           ← API Gateway Lambda handler
│   ├── nexus-channel-setup/handler.py ← Channel onboarding workflow Lambda
│   ├── nexus-brand-designer/handler.py← AI brand kit generation Lambda
│   ├── nexus-logo-gen/handler.py      ← Nova Canvas logo generation Lambda
│   └── nexus-intro-outro/handler.py   ← Intro/outro clip generation Lambda
│
├── statemachine/
│   ├── nexus_pipeline.asl.json        ← Main pipeline Step Functions ASL definition
│   └── nexus_channel_setup.asl.json   ← Channel setup Step Functions ASL definition
│
├── infrastructure/
│   ├── app.py                         ← CDK entry point
│   ├── nexus_stack.py                 ← CDK stack (all AWS resources)
│   ├── cdk.json                       ← CDK configuration
│   └── requirements.txt               ← CDK Python deps (aws-cdk-lib, constructs)
│
├── profiles/
│   ├── documentary.json
│   ├── finance.json
│   └── entertainment.json
│
├── luts_generated/                    ← Pre-built .cube LUT colour-grading files
│   ├── cinematic_teal_orange.cube
│   ├── cold_blue_corporate.cube
│   ├── high_contrast.cube
│   ├── punchy_vibrant_warm.cube
│   └── vintage_sepia.cube
│
├── dashboard/
│   └── index.html                     ← Single-file React monitoring dashboard
│
└── scripts/
    ├── setup_aws.py                   ← Bootstrap AWS resources (S3, IAM, Secrets Manager)
    ├── setup_luts.py                  ← Generate + upload .cube LUT colour-grading files
    ├── upload_sfx.py                  ← Download CC0 SFX from Freesound → upload to S3
    ├── test_connections.py            ← Verify all external services are reachable
    ├── test_check_external.py         ← External API health checker unit tests
    ├── check_external.py              ← Check external API health
    ├── orchestrator.py                ← Local Docker pipeline orchestrator
    ├── approve_upload.py              ← Manually approve YouTube uploads
    └── resume_run.py                  ← Resume a failed pipeline run from a specific step
```

---

## Prerequisites

| Tool | Min version | Install |
|------|-------------|---------|
| **AWS CLI** | 2.x | `brew install awscli` |
| **Node.js** | 20+ | `brew install node` |
| **Python** | 3.12+ | `brew install python@3.12` |
| **Docker** | 24+ | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| **AWS CDK** | 2.100+ | Auto-installed by `deploy.sh` or `npm i -g aws-cdk` |

### AWS Account Requirements

- IAM user with admin permissions (or scoped: S3, Lambda, IAM, Secrets Manager, Step Functions, API Gateway, CloudFront, EventBridge, CloudWatch, Bedrock, MediaConvert, ECR)
- **Bedrock models enabled** in your region (`us-east-1`):
  - `us.anthropic.claude-3-sonnet-20240229-v1:0`
  - `us.anthropic.claude-3-5-sonnet-20241022-v2:0`

---

## Quick Start — Full AWS Deployment

### 1. Clone & configure

```bash
git clone <repo-url> && cd automation
cp env.exemple .env
```

Edit `.env` and fill in **at minimum**:

```dotenv
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
PERPLEXITY_API_KEY=pplx-...
ELEVENLABS_API_KEY=sk_...
PEXELS_API_KEY=...
PIXABAY_API_KEY=...
FREESOUND_API_KEY=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DB_PASSWORD=your_secure_password
```

### 2. Deploy everything

```bash
chmod +x deploy.sh
./deploy.sh
```

This single command:

1. Validates AWS credentials
2. Installs CDK CLI (if needed)
3. Bootstraps AWS resources via Docker (S3 buckets, IAM roles, Secrets Manager)
4. Builds Lambda layers (API deps + ffmpeg static binaries)
5. Copies shared utils into each Lambda
6. Creates CDK venv & installs CDK Python deps
7. Bootstraps CDK (`cdk bootstrap`)
8. Deploys the full `NexusCloud` CloudFormation stack
9. Uploads channel profiles to S3
10. Deploys the dashboard with injected API URL
11. Updates `.env` with deployed ARNs

At the end you'll see:

```
🚀  Nexus Cloud deployed successfully!
  API URL:       https://xxxxxxxx.execute-api.us-east-1.amazonaws.com/prod/
  Dashboard:     https://dxxxxxxxxx.cloudfront.net
  State Machine: arn:aws:states:us-east-1:XXXX:stateMachine:nexus-pipeline
```

### 3. Test it

```bash
# Health check
curl https://<your-api-url>/prod/health

# Dry run (no AI calls, no YouTube upload — validates plumbing)
curl -X POST https://<your-api-url>/prod/run \
  -H 'Content-Type: application/json' \
  -d '{"niche":"technology","profile":"documentary","dry_run":true}'

# Full run
curl -X POST https://<your-api-url>/prod/run \
  -H 'Content-Type: application/json' \
  -d '{"niche":"obscure history","profile":"documentary","dry_run":false}'
```

Or via AWS CLI:

```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"niche":"technology","profile":"documentary","dry_run":true}'
```

### 4. Check status

```bash
# Via API
curl https://<your-api-url>/prod/status/<run_id>

# Get output URLs (video, thumbnail, metadata)
curl https://<your-api-url>/prod/outputs/<run_id>
```

### 5. Resume a failed run

If a run fails mid-pipeline, resume it from where it stopped (artifacts already in S3 are reused):

```bash
# Auto-detect the failed step and resume
curl -X POST https://<your-api-url>/prod/resume \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>"}'

# Resume from a specific step (Research|Script|AudioVisuals|Editor|Thumbnail|Notify)
curl -X POST https://<your-api-url>/prod/resume \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>","resume_from":"Editor"}'

# Or use the helper script
python scripts/resume_run.py <run_id>
```

---

## Local Development with Docker

> Full guide: [dockeruse.md](dockeruse.md)

```bash
# Start everything (Postgres + AWS bootstrap + 9 Lambda containers)
docker compose up --build

# Run connectivity tests (checks all 18 services)
docker compose --profile test run --rm test-connections

# Invoke research Lambda locally
curl -s -X POST http://localhost:9001/2015-03-31/functions/function/invocations \
  -d '{"niche":"obscure history","profile":"documentary","dry_run":true}'

# Stop & clean up
docker compose down -v
```

### Local Service Ports

| Service | Port | Runtime |
|---------|------|---------|
| Research | 9001 | Lambda `nexus-research` |
| Script | 9002 | Lambda `nexus-script` |
| Audio | 9003 | Fargate `nexus-audio` |
| Visuals | 9004 | Fargate `nexus-visuals` |
| Editor | 9005 | Fargate `nexus-editor` |
| Thumbnail | 9006 | Lambda `nexus-thumbnail` |
| Upload | 9007 | Lambda `nexus-upload` |
| Notify | 9008 | Lambda `nexus-notify` |
| API | 9009 | Lambda `nexus-api` |
| Orchestrator | 3000 | — |

---

## Running Tests

```bash
pip install -r requirements.txt
python -m pytest -v
```

Tests included:

| File | What it tests |
|------|--------------|
| `test_repair.py` | JSON repair logic in `nexus-script` (truncated LLM output recovery) |
| `test_drawtext.py` | FFmpeg drawtext escaping in `nexus-editor` (special chars, quotes, colons) |
| `scripts/test_connections.py` | End-to-end connectivity to all 18 external services |
| `scripts/test_check_external.py` | External API health checker |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (Step Functions + S3 reachability) |
| `POST` | `/run` | Start a new pipeline run |
| `POST` | `/resume` | Resume a failed run from a specific step |
| `GET` | `/status/{run_id}` | Execution status, per-step timeline, progress % |
| `GET` | `/outputs/{run_id}` | Pre-signed S3 URLs for video, thumbnails, error logs |

**POST /run body:**
```json
{ "niche": "string", "profile": "documentary|finance|entertainment", "dry_run": false }
```

**POST /resume body:**
```json
{ "run_id": "string", "resume_from": "Research|Script|AudioVisuals|Editor|Thumbnail|Notify" }
```

---

## Required Secrets (AWS Secrets Manager)

All secrets are created automatically by `setup_aws.py` from your `.env` values. The pipeline **never reads API keys from environment variables** — all are fetched from Secrets Manager at Lambda cold-start and cached in-memory.

| Secret Name | JSON Key(s) | Source |
|-------------|------------|--------|
| `nexus/perplexity_api_key` | `api_key` | [Perplexity](https://www.perplexity.ai/settings/api) |
| `nexus/elevenlabs_api_key` | `api_key` | [ElevenLabs](https://elevenlabs.io) |
| `nexus/pexels_api_key` | `api_key` | [Pexels](https://www.pexels.com/api/) |
| `nexus/youtube_credentials` | `client_id`, `client_secret`, `refresh_token` | Google Cloud Console OAuth2 |
| `nexus/discord_webhook_url` | `url` | Discord → Server Settings → Integrations → Webhooks |
| `nexus/db_credentials` | `host`, `port`, `dbname`, `user`, `password` | Your PostgreSQL / RDS instance |

---

## Channel Profiles

Stored in `profiles/` and uploaded to the `nexus-config` S3 bucket during deploy.

| Profile | Duration | Transition | Colour Grade | Music Mood |
|---------|----------|------------|-------------|-----------|
| `documentary` | 10–16 min | dissolve | cinematic warm | tension / atmospheric |
| `finance` | 8–14 min | cut | clean corporate | corporate upbeat |
| `entertainment` | 6–12 min | zoom punch | punchy vibrant | energetic hype |

---

## AI Models Used

| Task | Model | Provider |
|------|-------|----------|
| Topic research | `sonar-pro` | Perplexity |
| Script (structure, depth, pacing, hooks) | `claude-3-sonnet` | AWS Bedrock |
| Finance fact-checking | `sonar-pro` | Perplexity |
| Thumbnail concept generation | `claude-3-sonnet` | AWS Bedrock |
| Thumbnail frame scoring | `claude-3-sonnet` (Vision) | AWS Bedrock |
| Logo / brand image generation | Nova Canvas | AWS Bedrock |
| Intro/outro video generation | Nova Reel | AWS Bedrock |
| Text-to-speech | `eleven_turbo_v2_5` | ElevenLabs |
| Video transcoding (>10 min) | MediaConvert | AWS |

---

## Cost Estimate per Run

> Approximate for a 12-minute documentary video.

| Service | Usage | Cost |
|---------|-------|------|
| Perplexity sonar-pro | ~2K tokens × 2 calls | ~$0.03 |
| Bedrock Claude 3 Sonnet | ~40K tokens | ~$0.60 |
| ElevenLabs TTS | ~15K characters | ~$0.60 |
| Lambda compute | 5 functions, ~15 min total | ~$0.05 |
| ECS Fargate (Audio + Visuals + Editor) | 4 vCPU / 16 GB × ~45 min | ~$0.30 |
| S3 storage + transfer | ~500 MB/run | ~$0.02 |
| MediaConvert | HD transcode | ~$0.05 |
| Step Functions | state transitions | ~$0.00 |
| **Total** | | **~$1.00–$1.65** |

---

## YouTube Upload — Manual Approval

By default `YOUTUBE_AUTO_PUBLISH=false`. Videos are saved to S3 for review.

```bash
# Review and upload manually
python scripts/approve_upload.py <run_id>
```

To enable auto-publish, set in `.env`:

```dotenv
YOUTUBE_AUTO_PUBLISH=true
YOUTUBE_CLIENT_ID=your_client_id
YOUTUBE_CLIENT_SECRET=your_secret
YOUTUBE_REFRESH_TOKEN=your_token
```

---

## Error Handling

- Every Lambda wraps logic in `try/except` → writes errors to `s3://nexus-outputs/{run_id}/errors/{step}.json`
- Every Fargate task exports identical error JSON to the same S3 path on failure
- Step Functions `.catch` on each state routes failures to `nexus-notify-error` → Discord alert
- LLM calls retry up to 3× with exponential backoff (`2^attempt` seconds)
- ElevenLabs failures retry with stripped pacing markers
- Failed runs can be resumed mid-pipeline via `POST /resume` or `scripts/resume_run.py`

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
- Lambda p95 duration for research, script, thumbnail, upload, notify
- Lambda error counts for all Lambda functions
- View in AWS Console → CloudWatch → Dashboards → `nexus-pipeline`

ECS Fargate tasks (audio, visuals, editor) can be monitored via their CloudWatch log groups:
- `/ecs/nexus-audio`
- `/ecs/nexus-visuals`
- `/ecs/nexus-editor`

---

## Teardown

```bash
cd infrastructure
source .venv/bin/activate
cdk destroy NexusCloud \
  -c account=<AWS_ACCOUNT_ID> \
  -c region=us-east-1
```

The dashboard bucket has `auto_delete_objects=True` so CDK can cleanly destroy it.

To also remove resources created by `setup_aws.py` (S3 buckets, IAM roles, Secrets Manager):

```bash
# Empty and delete S3 buckets
for b in nexus-assets-<ACCOUNT> nexus-outputs nexus-config-<ACCOUNT>; do
  aws s3 rm "s3://$b" --recursive && aws s3 rb "s3://$b"
done

# Delete secrets (immediate, no recovery)
for s in nexus/perplexity_api_key nexus/elevenlabs_api_key nexus/pexels_api_key \
         nexus/youtube_credentials nexus/discord_webhook_url nexus/db_credentials; do
  aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery
done
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `deploy.sh` fails at credentials | Ensure `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set in `.env` |
| Bedrock `AccessDeniedException` | Enable Claude 3 Sonnet in AWS Console → Bedrock → Model access (region: `us-east-1`) |
| S3 bucket name already taken | `setup_aws.py` auto-appends your account ID; check `.env` for actual bucket names |
| `cdk destroy` fails on dashboard bucket | Fixed: `auto_delete_objects=True` is now set. For old stacks, empty the bucket first |
| MediaConvert role creation fails | IAM user needs `iam:CreateRole` + `iam:PutRolePolicy` permissions |
| YouTube upload fails | Set `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` with `youtube.upload` scope |
| Docker build hangs on ffmpeg download | Check internet / proxy; the static binary is ~70 MB from johnvansickle.com |
| Fargate task timeout | Audio, Visuals, and Editor are the heaviest; increase `cpu`/`memory_limit_mib` in `nexus_stack.py` |
| Fargate "exec format error" | Ensure Docker images are built for `linux/arm64` — CDK sets `LINUX_ARM64` automatically |
| EFS mount fails in Fargate | Check `scratch_fs_sg` security group allows NFS (port 2049) from the VPC CIDR |
| Pipeline stuck mid-run | Resume from the failed step: `python scripts/resume_run.py <run_id>` |

---

## License

Private — All rights reserved.
