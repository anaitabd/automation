# Docker Usage Guide — Nexus Cloud

Complete guide to running the Nexus Cloud pipeline locally via Docker using
**real AWS services** (no LocalStack).

---

## Prerequisites

| Tool             | Min version | Install                                 |
|------------------|-------------|------------------------------------------|
| Docker           | 24+         | [docker.com](https://docs.docker.com/)   |
| Docker Compose   | 2.20+       | Bundled with Docker Desktop              |
| AWS account      | —           | With Bedrock, S3, IAM, Secrets Manager, MediaConvert enabled |

---

## 1. Configure `.env`

Copy the example and fill in your credentials:

```bash
cp env.exemple .env
```

**Required** — set these two (the rest is auto-populated by setup):

```dotenv
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=wJal...
```

The `.env` file already contains all API keys. See `env.exemple` for the full
template with descriptions.

> **Important:** `YOUTUBE_AUTO_PUBLISH=false` by default. Videos are saved to S3
> for manual approval instead of being uploaded to YouTube automatically.

---

## 2. Build & Start

```bash
docker compose up --build
```

This starts (in order):

| Service         | Port  | Description                              |
|-----------------|-------|------------------------------------------|
| `postgres`      | 5432  | PostgreSQL 16 database                   |
| `setup-aws`     | —     | Bootstraps AWS resources (runs once)     |
| `nexus-research` | 9001  | Research Lambda container                |
| `nexus-script`  | 9002  | Script generation Lambda                 |
| `nexus-audio`   | 9003  | Audio/TTS Lambda                         |
| `nexus-visuals` | 9004  | Visual sourcing Lambda                   |
| `nexus-editor`  | 9005  | Video assembly Lambda                    |
| `nexus-thumbnail` | 9006 | Thumbnail generation Lambda             |
| `nexus-upload`  | 9007  | YouTube upload Lambda (manual approval)  |
| `nexus-notify`  | 9008  | Discord + DB notification Lambda         |
| `nexus-api`     | 9009  | API Gateway handler Lambda               |

### What `setup-aws` does automatically

1. Detects your **AWS Account ID** via STS
2. Creates **S3 buckets**: `nexus-assets`, `nexus-outputs`, `nexus-config`
3. Creates **IAM role** for MediaConvert (`nexus-mediaconvert-role`) with S3
   trust policy — writes the ARN back to `.env`
4. Creates/updates all **Secrets Manager** secrets from `.env` values
5. Uploads **channel profiles** (`documentary.json`, `finance.json`,
   `entertainment.json`) to `nexus-config` bucket

After setup completes, your `.env` will have `AWS_ACCOUNT_ID` and
`MEDIACONVERT_ROLE_ARN` auto-populated.

---

## 3. Run Connectivity Test

Verify every external service is reachable:

```bash
docker compose --profile test run --rm test-connections
```

This checks:

| #  | Service                   | What it verifies                        |
|----|---------------------------|-----------------------------------------|
| 1  | AWS STS                   | Credentials are valid                   |
| 2  | S3 (×3 buckets)          | Buckets exist and are accessible        |
| 3  | Secrets Manager (×7)     | All secrets exist with correct keys     |
| 4  | Bedrock (claude-3-sonnet) | Model invocation works                  |
| 5  | Perplexity API            | sonar-pro responds                      |
| 6  | ElevenLabs API            | API key is valid                        |
| 7  | Pexels API                | Video search works                      |
| 8  | Discord Webhook           | Test embed delivered                    |
| 9  | PostgreSQL                | Connection + `SELECT 1`                 |
| 10 | MediaConvert              | Endpoint reachable                      |

Example output:

```
============================================================
  Nexus Cloud — Service Connectivity Test
============================================================

  ✅  AWS STS (credentials)  — Account=670294435884
  ✅  S3 bucket: nexus-assets-670294435884
  ✅  S3 bucket: nexus-outputs
  ✅  S3 bucket: nexus-config-670294435884
  ✅  Secret: nexus/perplexity_api_key  — keys=['api_key']
  ✅  Secret: nexus/elevenlabs_api_key  — keys=['api_key']
  ...
  ✅  Bedrock (claude-3-sonnet)  — response='Hello.'
  ✅  Perplexity API (sonar-pro)  — response='...'
  ✅  PostgreSQL  — postgres:5432/nexus
  ✅  MediaConvert  — endpoint=https://mediaconvert.us-east-1.amazonaws.com

============================================================
  Results: 18/18 passed, 0 failed
============================================================
```

> **Note:** S3 bucket names may get an account-ID suffix (e.g.
> `nexus-assets-670294435884`) if the base name is globally taken. The setup
> script handles this automatically and writes the actual names back to `.env`.
>
> If ElevenLabs, Pexels, or Discord fail, update the corresponding API keys in
> `.env` and re-run `docker compose run --rm setup-aws` to push them to Secrets
> Manager.

---

## 4. Trigger a Dry Run (no AI calls)

```bash
curl -s -X POST http://localhost:9001/2015-03-31/functions/function/invocations \
  -d '{"niche": "obscure history", "profile": "documentary", "dry_run": true}' | python -m json.tool
```

This invokes the Research lambda without making real API calls.

---

## 5. Trigger a Real Pipeline Run

Invoke each Lambda step in sequence:

```bash
# Step 1: Research
curl -s -X POST http://localhost:9001/2015-03-31/functions/function/invocations \
  -d '{"niche": "obscure history", "profile": "documentary", "dry_run": false}'

# Step 2: Script (use run_id from step 1)
curl -s -X POST http://localhost:9002/2015-03-31/functions/function/invocations \
  -d '{"run_id": "<RUN_ID>", "profile": "documentary", "research_s3_key": "<KEY>", "selected_topic": "...", "angle": "...", "trending_context": "...", "dry_run": false}'

# ... continue through ports 9003-9008
```

---

## 6. YouTube Upload — Manual Approval

Since `YOUTUBE_AUTO_PUBLISH=false`, the upload Lambda saves metadata to S3
instead of uploading. To manually approve and upload:

### Option A: CLI script

```bash
python scripts/approve_upload.py <run_id>
```

This will:
1. Show you the pending video title, profile, duration
2. Ask for confirmation
3. Download video + thumbnail from S3
4. Upload to YouTube as **private**
5. Print the YouTube URL

### Option B: Change to auto-publish

Set in `.env`:

```dotenv
YOUTUBE_AUTO_PUBLISH=true
YOUTUBE_CLIENT_ID=your_client_id
YOUTUBE_CLIENT_SECRET=your_secret
YOUTUBE_REFRESH_TOKEN=your_refresh_token
```

Then restart:

```bash
docker compose up --build
```

---

## 7. Model Configuration

| Component       | Model                                         |
|-----------------|-----------------------------------------------|
| Research        | `us.anthropic.claude-3-sonnet-20240229-v1:0`  |
| Script          | `us.anthropic.claude-3-sonnet-20240229-v1:0`  |
| Thumbnail       | `us.anthropic.claude-3-sonnet-20240229-v1:0`  |
| TTS             | `eleven_turbo_v2_5` (ElevenLabs)              |
| Video search    | Pexels + Archive.org                          |
| Video encoding  | AWS MediaConvert (for videos >10 min)         |

---

## 8. Stop Everything

```bash
docker compose down
```

To also remove volumes (PostgreSQL data):

```bash
docker compose down -v
```

---

## 9. Troubleshooting

### `setup-aws` fails with credentials error

```
ERROR: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in .env
```

→ Make sure both are filled in `.env` (not empty).

### Bedrock returns `AccessDeniedException`

→ Your IAM user needs `bedrock:InvokeModel` permission. Also ensure
  `us.anthropic.claude-3-sonnet-20240229-v1:0` is enabled in your AWS Bedrock
  console for `us-east-1`.

### MediaConvert role creation fails

→ Your IAM user needs `iam:CreateRole`, `iam:PutRolePolicy` permissions. The
  `setup-aws` service creates the role automatically — you don't need to do it
  manually.

### S3 bucket name already taken

→ S3 bucket names are globally unique. The `setup-aws` script automatically
  appends your AWS account ID if the base name is taken (e.g.
  `nexus-assets-670294435884`). The actual names are written back to `.env`.
  If you want custom names, change `ASSETS_BUCKET`, `OUTPUTS_BUCKET`,
  `CONFIG_BUCKET` in `.env` before first run.

### YouTube upload fails

→ Ensure `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, and
  `YOUTUBE_REFRESH_TOKEN` are set. The refresh token must be from a Google
  Cloud OAuth2 flow with `youtube.upload` scope.

---

## Architecture (Docker Mode)

```
┌─────────────────────────────────────────────────────┐
│                    docker compose                    │
│                                                     │
│  ┌──────────┐   ┌──────────────────────────────┐   │
│  │ postgres │   │         setup-aws             │   │
│  │  :5432   │   │  • S3 buckets                 │   │
│  └──────────┘   │  • IAM MediaConvert role      │   │
│                 │  • Secrets Manager             │   │
│                 │  • Profile uploads             │   │
│                 └──────────────────────────────────┘ │
│                                                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐ │
│  │research │ │ script  │ │  audio  │ │ visuals  │ │
│  │ :9001   │ │ :9002   │ │ :9003   │ │  :9004   │ │
│  └─────────┘ └─────────┘ └─────────┘ └──────────┘ │
│                                                     │
│  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐ │
│  │ editor  │ │thumbnail │ │ upload │ │  notify  │ │
│  │ :9005   │ │  :9006   │ │ :9007  │ │  :9008   │ │
│  └─────────┘ └──────────┘ └────────┘ └──────────┘ │
│                                                     │
│  ┌─────────┐                                       │
│  │   api   │  ← all talk to real AWS services       │
│  │ :9009   │                                       │
│  └─────────┘                                       │
└─────────────────────────────────────────────────────┘
         │
         ▼
   AWS (us-east-1)
   • S3 • Secrets Manager • Bedrock • MediaConvert • IAM
```

