# Architecture — Nexus Cloud Automation Pipeline

## System Overview

```
                          ┌──────────────────┐
                          │   EventBridge    │  (scheduled, disabled by default)
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │   API Gateway    │  POST /run · GET /status · GET /outputs
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │  nexus-api       │  Lambda — starts & queries Step Functions
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │  Step Functions  │  nexus-pipeline (sequential state machine)
                          └────────┬─────────┘
                                   │
        ┌──────────┬───────────────┼───────────────┬──────────┬──────────┐
        ▼          ▼               ▼               ▼          ▼          ▼
   Research → Script → Audio → Visuals → Editor → Thumbnail → Upload → Notify
        │          │               │               │          │          │
        └──────────┴───────────────┴───────────────┴──────────┴──────────┘
                                   │
               ┌───────────────────┼───────────────────┐
               ▼                   ▼                   ▼
        nexus-assets         nexus-outputs       nexus-config
          (S3)                  (S3)               (S3)
    raw audio/video         final MP4 +         channel profiles
    downloaded clips        thumbnail           & LUTs
```

On any Lambda failure the Step Functions `.catch` handler invokes `nexus-notify-error`, which sends a Discord alert and logs the run to PostgreSQL.

---

## Lambda Functions

| # | Function | Memory | Timeout | Description |
|---|----------|--------|---------|-------------|
| — | `nexus-api-handler` | 256 MB | 30 s | API Gateway integration — triggers and queries Step Functions executions |
| 1 | `nexus-research` | 512 MB | 5 min | Calls Perplexity sonar-pro for trending topics and AWS Bedrock Claude for topic refinement |
| 2 | `nexus-script` | 1024 MB | 15 min | Multi-pass script generation with Bedrock Claude; includes JSON repair for truncated LLM output |
| 3 | `nexus-audio` | 2048 MB | 15 min | ElevenLabs TTS, ffmpeg audio EQ, Pixabay background music mixing and SFX |
| 4 | `nexus-visuals` | 3008 MB | 15 min | Pexels/Archive.org footage download with CLIP semantic scoring (Docker image) |
| 5 | `nexus-editor` | 3008 MB | 15 min | Beat-synced video assembly, ffmpeg overlay rendering, AWS MediaConvert HD transcode (Docker image) |
| 6 | `nexus-thumbnail` | 1024 MB | 5 min | Bedrock Vision frame scoring, Claude concept generation, ffmpeg composite thumbnail render |
| 7 | `nexus-upload` | 512 MB | 10 min | YouTube Data API v3 OAuth2 upload (manual approval by default) |
| 8 | `nexus-notify` | 256 MB | 1 min | Discord webhook notification and PostgreSQL run logging |

---

## Data Flow

1. **Trigger** — `POST /run` with `{"niche": "...", "profile": "...", "dry_run": false}` reaches API Gateway → `nexus-api-handler` → starts a Step Functions execution.
2. **Research** (`nexus-research`) — Perplexity searches for trending topics; Bedrock selects the best angle. Writes `research.json` to `nexus-outputs/{run_id}/`.
3. **Script** (`nexus-script`) — Reads `research.json`, calls Bedrock Claude through multiple passes to produce a structured script JSON. Writes `script.json`.
4. **Audio** (`nexus-audio`) — Reads `script.json`, generates per-section TTS audio via ElevenLabs, downloads background music, mixes everything with ffmpeg. Writes audio files to `nexus-assets/{run_id}/audio/`.
5. **Visuals** (`nexus-visuals`) — Reads `script.json` visual cues, downloads stock clips from Pexels/Archive.org, scores them with CLIP, and stores the best clips in `nexus-assets/{run_id}/clips/`.
6. **Editor** (`nexus-editor`) — Assembles the final video: syncs clips to audio beats, renders ffmpeg drawtext overlays, optionally dispatches AWS MediaConvert for long videos. Writes `final.mp4` to `nexus-outputs/{run_id}/`.
7. **Thumbnail** (`nexus-thumbnail`) — Scores video frames with Bedrock Vision, generates a thumbnail concept with Claude, renders a composite with ffmpeg. Writes `thumbnail.jpg` to `nexus-outputs/{run_id}/`.
8. **Upload** (`nexus-upload`) — Uploads `final.mp4` and `thumbnail.jpg` to YouTube via OAuth2. Stores the YouTube URL in `nexus-outputs/{run_id}/result.json`.
9. **Notify** (`nexus-notify`) — Posts a Discord embed with the run result and logs metadata to PostgreSQL.

---

## S3 Buckets

| Bucket | Purpose |
|--------|---------|
| `nexus-assets-{account}` | Intermediate assets: downloaded clips, generated audio, SFX, LUTs |
| `nexus-outputs-{account}` | Final outputs per run: JSON metadata, MP4, thumbnail, errors |
| `nexus-config-{account}` | Channel profiles (JSON), colour LUT files (.cube) |
| `nexus-dashboard-{account}` | Static React monitoring dashboard (public, CloudFront-served) |

---

## Step Functions Workflow

The state machine (`nexus-pipeline`) is defined in `statemachine/nexus_pipeline.asl.json`. It executes steps sequentially, passing the accumulated state (`run_id`, S3 paths, metadata) between each Lambda invocation. Each step has a `.catch` block that routes failures to `nexus-notify-error`.

```
StartExecution
  └─ Research      (Task)
  └─ Script        (Task)
  └─ Audio         (Task)
  └─ Visuals       (Task)
  └─ Editor        (Task)
  └─ Thumbnail     (Task)
  └─ Upload        (Task)
  └─ Notify        (Task)
  └─ [Any failure] → NotifyError (Task)
```

---

## Infrastructure

All AWS resources are defined as a single CDK stack (`NexusCloud`) in `infrastructure/nexus_stack.py`. Deployed via `deploy.sh` or `cdk deploy`. Includes:

- API Gateway REST API
- Step Functions state machine with CloudWatch logging
- 9 Lambda functions (ZIP-based and Docker-based)
- 2 Lambda layers (API deps, static ffmpeg binaries)
- 4 S3 buckets
- CloudFront distribution for the dashboard
- EventBridge scheduled rule (disabled by default)
- CloudWatch dashboard with Lambda duration and error metrics
- IAM roles scoped to least privilege per function
- AWS Secrets Manager secrets for all external API keys
