# Nexus Cloud — Serverless YouTube Automation Pipeline

Turn any niche keyword into a fully produced, uploaded YouTube video using the
best available AI models — entirely on AWS, entirely serverless.

---

## Architecture Overview

```
API Gateway → Step Functions → 8 Lambda Functions → YouTube
                   │
              S3 (3 buckets): nexus-assets · nexus-outputs · nexus-config
```

| Step | Lambda | Purpose |
|------|--------|---------|
| 1 | `nexus-research` | Perplexity sonar-pro + Bedrock claude-opus-4-0 → best topic/angle |
| 2 | `nexus-script` | 5-pass script (Bedrock claude-opus-4-0 + Perplexity fact-check) |
| 3 | `nexus-audio` | ElevenLabs TTS + ffmpeg EQ + Pixabay music + SFX |
| 4 | `nexus-visuals` | Storyblocks / Pexels / Archive.org / Runway + CLIP scoring |
| 5 | `nexus-editor` | Beat-sync assembly + AWS MediaConvert + overlays |
| 6 | `nexus-thumbnail` | Bedrock Vision frame scoring + Claude concepts + ffmpeg render |
| 7 | `nexus-upload` | YouTube Data API v3 OAuth2 private upload |
| 8 | `nexus-notify` | Discord webhook + PostgreSQL run logging |

---

## File Structure

```
automation/
├── lambdas/
│   ├── nexus-research/handler.py
│   ├── nexus-script/handler.py
│   ├── nexus-audio/handler.py
│   ├── nexus-visuals/handler.py
│   ├── nexus-editor/handler.py
│   ├── nexus-thumbnail/handler.py
│   ├── nexus-upload/handler.py
│   ├── nexus-notify/handler.py
│   └── nexus-api/handler.py          ← API Gateway handler
├── statemachine/
│   └── nexus_pipeline.asl.json       ← Step Functions ASL definition
├── infrastructure/
│   ├── app.py                        ← CDK entry point
│   ├── nexus_stack.py                ← CDK stack (all resources)
│   └── cdk.json
├── profiles/
│   ├── documentary.json
│   ├── finance.json
│   └── entertainment.json
├── dashboard/
│   └── index.html                    ← Single-file React dashboard
└── scripts/
    ├── setup_luts.py                 ← Generate + upload .cube LUT files
    └── upload_sfx.py                 ← Download CC0 SFX from Freesound + upload
```

---

## Prerequisites

- AWS CLI configured (`aws configure`)
- AWS CDK v2: `npm install -g aws-cdk`
- Python 3.12+
- `pip install aws-cdk-lib constructs`

---

## Required Secrets (AWS Secrets Manager)

Create each secret before deploying. The pipeline never uses environment
variables for API keys — all secrets are fetched at Lambda cold-start and
cached in-memory.

| Secret Name | Key(s) in JSON | Where to get it |
|-------------|----------------|-----------------|
| `nexus/perplexity_api_key` | `api_key` | [Perplexity Labs](https://www.perplexity.ai/settings/api) |
| `nexus/elevenlabs_api_key` | `api_key` | [ElevenLabs](https://elevenlabs.io) |
| `nexus/pexels_api_key` | `api_key`, `pixabay_key` | [Pexels](https://www.pexels.com/api/), [Pixabay](https://pixabay.com/api/docs/) |
| `nexus/storyblocks_api_key` | `api_key`, `private_key` | [Storyblocks API](https://www.storyblocks.com/api) |
| `nexus/runwayml_api_key` | `api_key` | [RunwayML](https://app.runwayml.com/settings/developer) |
| `nexus/youtube_credentials` | `client_id`, `client_secret`, `refresh_token` | Google Cloud Console OAuth2 |
| `nexus/discord_webhook_url` | `url` | Discord Server → Integrations → Webhooks |
| `nexus/db_credentials` | `host`, `port`, `dbname`, `user`, `password` | RDS / Aurora Serverless v2 |

### Quick secret creation example

```bash
aws secretsmanager create-secret \
  --name nexus/perplexity_api_key \
  --secret-string '{"api_key":"pplx-..."}'
```

---

## Deployment

### 1. Build Lambda layers

The CDK stack references three Lambda layers. Build them before deploying:

```bash
# ffmpeg layer (static binary for AL2023 arm64)
mkdir -p layers/ffmpeg/bin
# Download from https://johnvansickle.com/ffmpeg/ (arm64 build) or compile
# Place ffmpeg and ffprobe binaries in layers/ffmpeg/bin/

# ml-layer
mkdir -p layers/ml/python
pip install sentence-transformers torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    librosa numpy scipy -t layers/ml/python

# api-layer
mkdir -p layers/api/python
pip install requests boto3 python-dotenv psycopg2-binary -t layers/api/python
```

### 2. Upload channel profiles to S3

```bash
# Create the config bucket first if deploying manually:
aws s3 mb s3://nexus-config

aws s3 cp profiles/documentary.json s3://nexus-config/documentary.json
aws s3 cp profiles/finance.json     s3://nexus-config/finance.json
aws s3 cp profiles/entertainment.json s3://nexus-config/entertainment.json
```

### 3. Deploy CDK stack

```bash
cd infrastructure
cdk bootstrap   # first time only
cdk deploy
```

### 4. Set up LUTs and SFX

```bash
# Generate and upload LUT files
python scripts/setup_luts.py --upload-to-s3

# Download CC0 SFX from Freesound and upload
export FREESOUND_API_KEY=your_freesound_key
python scripts/upload_sfx.py
```

### 5. Deploy dashboard

```bash
# Get the dashboard bucket name from CDK output
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name NexusCloud \
  --query 'Stacks[0].Outputs[?OutputKey==`DashboardBucket`].OutputValue' \
  --output text)

# Edit dashboard/index.html → set __NEXUS_API_BASE__ to your API Gateway URL
# Then upload:
aws s3 cp dashboard/index.html s3://$BUCKET/index.html \
  --content-type text/html --cache-control no-cache
```

---

## Triggering a Pipeline Run

### CLI

```bash
# Get the state machine ARN from CDK output or describe-stacks:
export STATE_MACHINE_ARN=$(aws cloudformation describe-stacks \
  --stack-name NexusCloud \
  --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' \
  --output text)

aws stepfunctions start-execution \
  --state-machine-arn $STATE_MACHINE_ARN \
  --input '{"niche": "obscure history", "profile": "documentary", "dry_run": false}'
```

### API

```bash
# Replace with your API Gateway URL from CDK output
export API_URL=https://xxxxxxxx.execute-api.us-east-1.amazonaws.com/prod

# Start a run
curl -X POST $API_URL/run \
  -H "Content-Type: application/json" \
  -d '{"niche": "obscure history", "profile": "documentary", "dry_run": false}'

# Check status (use run_id from above)
curl $API_URL/status/<run_id>

# Get output URLs
curl $API_URL/outputs/<run_id>
```

### Dry Run (no AI calls, no YouTube upload)

```bash
aws stepfunctions start-execution \
  --state-machine-arn $STATE_MACHINE_ARN \
  --input '{"niche": "test", "profile": "finance", "dry_run": true}'
```

---

## Channel Profiles

| Profile | Duration | CPM | Transition | Colour Grade | Music Mood |
|---------|----------|-----|------------|-------------|-----------|
| documentary | 10–16 min | 8 | dissolve | cinematic_warm | tension_atmospheric |
| finance | 8–14 min | 16 | cut | clean_corporate | corporate_upbeat_subtle |
| entertainment | 6–12 min | 28 | zoom_punch | punchy_vibrant | energetic_hype |

---

## Model Selection (hardcoded)

| Task | Model | Provider |
|------|-------|----------|
| Research | sonar-pro | Perplexity |
| Script structure / depth / visual cues / pacing | claude-opus-4-0 | AWS Bedrock |
| Hook rewrite | claude-opus-4-0 | AWS Bedrock |
| Finance fact-check | sonar-pro | Perplexity |
| Thumbnail concepts | claude-opus-4-0 | AWS Bedrock |
| Thumbnail frame scoring | claude-opus-4-0 (Vision) | AWS Bedrock |
| Text-to-speech | eleven_turbo_v2_5 | ElevenLabs |
| AI video generation (fallback) | gen3a_turbo | Runway ML |
| Video transcoding (>10 min) | MediaConvert | AWS |

---

## Cost Estimate per Pipeline Run

> Estimates are approximate based on 12-minute documentary video.

| Service | Usage | Est. cost |
|---------|-------|-----------|
| Perplexity sonar-pro | ~2K tokens × 2 calls | ~$0.03 |
| Bedrock claude-opus-4-0 | ~40K tokens | ~$0.60 |
| ElevenLabs eleven_turbo_v2_5 | ~15K chars | ~$0.60 |
| Runway gen3a_turbo (if triggered) | per clip | ~$0.25 |
| Lambda compute | 8 functions, ~45 min total | ~$0.15 |
| S3 storage + transfer | ~500 MB per run | ~$0.02 |
| MediaConvert (if >10 min) | HD transcode | ~$0.05 |
| Step Functions | 8 state transitions | ~$0.00 |
| **Total** | | **~$1.25–$2.00** |

---

## Error Handling

- Every Lambda wraps its logic in `try/except` and writes errors to
  `s3://nexus-outputs/{run_id}/errors/{step}.json`
- Step Functions `.catch` on each state routes failures to `nexus-notify-error`
  which sends a Discord alert
- LLM calls retry up to 3× with exponential backoff (`2^attempt` seconds)
- Runway generation times out after 90 s → falls back to best Pexels clip
- ElevenLabs failure → retries with stripped pacing markers

---

## Scheduled Runs (EventBridge)

An EventBridge rule is created but **disabled** by default. Enable and configure
via the AWS Console or CDK context:

```bash
aws events enable-rule --name nexus-pipeline-schedule
```

Edit the rule target input in the console or CDK to set your desired
`niche` and `profile` for automated twice-daily runs.

---

## CloudWatch Dashboard

A CloudWatch dashboard named `nexus-pipeline` is automatically created showing:
- Lambda p95 durations for all 8 functions
- Lambda error counts
- Step Functions execution history (via Console link)