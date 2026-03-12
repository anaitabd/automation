# AGENTS.md

## Big Picture Architecture
- Cloud entrypoint is API Gateway -> `lambdas/nexus-api/handler.py` (`/run`, `/resume`, `/status/{run_id}`, `/outputs/{run_id}`).
- Authoritative orchestration is `statemachine/nexus_pipeline.asl.json`, wired by Terraform (`terraform/modules/orchestration/`) using `templatefile()` for ARN injection.
- Heavy media steps run on ECS Fargate (`nexus-audio`, `nexus-visuals`, `nexus-editor`, `nexus-shorts`) with EFS scratch at `/mnt/scratch`; lighter steps stay Lambda.
- `nexus-shorts` is an optional Fargate module producing 15s/30s/45s/60s vertical short-form videos in parallel with `nexus-editor`. Enabled per-run via `generate_shorts: true` in pipeline input; failures never block the main video.
- Bucket roles are split: `OUTPUTS_BUCKET` (JSON artifacts, review assets, errors), `ASSETS_BUCKET` (media intermediates), `CONFIG_BUCKET` (profile JSON).
- Infrastructure is defined in `terraform/` (8 modules: storage, secrets, networking, identity, compute, orchestration, api, observability). Legacy CDK stack is in `infrastructure/nexus_stack.py` (frozen).

## Critical Workflows
- **Terraform deploy**: `bash terraform/scripts/deploy_tf.sh` from repo root — reads `.env`, copies shared utils, builds Lambda layers + ECS images, generates `terraform.tfvars`, runs `terraform apply`.
- **CDK deploy (legacy/frozen)**: `./deploy.sh` — still functional but superseded by Terraform path.
- `deploy_tf.sh` copies `lambdas/nexus_pipeline_utils.py` into every `lambdas/nexus-*/` folder before packaging; edit the root shared file, not per-lambda copies.
- Local pipeline path is `docker compose up --build` plus `orchestrator` (`scripts/orchestrator.py`) for a Step-Functions-like flow and SSE dashboard on port 3000.
- Tests are Python/pytest-centric: `pytest.ini` scopes to `scripts`; AWS integration checks require `RUN_AWS_TESTS=1`.
- Practical fast loop: run `python -m pytest scripts/test_check_external.py -v` before touching external-API check logic.
- Post-deploy validation: `cd terraform && bash scripts/validate_deploy.sh` (checks API, SFN, S3, Lambda, ECS, dry-run execution).

## Codebase Conventions (Project-Specific)
- Step handlers preserve state keys (`run_id`, `profile`, `dry_run`, plus step outputs) because ASL `ResultSelector`/`Pass` states expect exact field names.
- Most lambdas cache Secrets Manager reads in module-level dicts (`_cache`) and avoid env-stored API keys.
- Error pattern is consistent: log, write `s3://<outputs>/{run_id}/errors/{step}.json`, then raise (example: `lambdas/nexus-research/handler.py`).
- Notifications are first-class: start/complete Discord messages come from `notify_step_start` / `notify_step_complete` in `lambdas/nexus_pipeline_utils.py`.
- Resume behavior is artifact-driven: both API and CLI (`scripts/resume_run.py`) infer next step from existing S3 keys.
- Shared media helpers live in `lambdas/shared/` (`nova_canvas.py`, `nova_reel.py`, `motion.py`); Fargate Dockerfiles copy them into the image (see `lambdas/nexus-shorts/Dockerfile`).

## Terraform Module Map
- `terraform/modules/storage/` — imports pre-existing S3 buckets, creates dashboard bucket, uploads profiles.
- `terraform/modules/secrets/` — manages all `nexus/*` Secrets Manager secrets with JSON payloads matching handler expectations.
- `terraform/modules/networking/` — default VPC lookup, EFS file system + access point, NFS security group.
- `terraform/modules/identity/` — all IAM roles (Lambda, ECS execution/task, MediaConvert, SFN, API) with least-privilege policies.
- `terraform/modules/compute/` — Lambda functions (zip packaging), ECS cluster, ECR repos, Fargate task definitions with EFS mounts.
- `terraform/modules/orchestration/` — Step Functions state machine via `templatefile()` on `statemachine/nexus_pipeline.asl.json`.
- `terraform/modules/api/` — API Gateway REST API (5 routes + CORS), CloudFront distribution for dashboard.
- `terraform/modules/observability/` — EventBridge schedule (disabled by default), CloudWatch dashboard with Lambda metrics.

## Integrations and Boundaries
- External services in active use: Perplexity, Bedrock, ElevenLabs, Pexels/Pixabay, Discord, PostgreSQL, YouTube OAuth upload.
- Canonical secret names are in `terraform/modules/secrets/` (e.g., `nexus/perplexity_api_key`, `nexus/db_credentials`, `nexus/freesound_api_key`).
- API lambda injects ECS subnet IDs via `ECS_SUBNETS` env; ASL ECS tasks consume `$.subnets` for `AwsvpcConfiguration`.
- SFN role uses wildcard `arn:aws:lambda:REGION:ACCOUNT:function:nexus-*` pattern to avoid circular module dependencies.

## High-Impact Gotchas
- Cloud and local flows are not identical: ASL routes `Thumbnail → Notify` (no Upload task), while `scripts/orchestrator.py` still includes an `Upload` step. Both now run Editor ∥ Shorts in parallel (`_PARALLEL_CONTENT_GROUP`).
- `README.md` references some tests (`test_repair.py`, `test_drawtext.py`) that are not present; current tests are mainly under `scripts/`.
- Do not edit generated artifacts (`infrastructure/cdk.out/`, `terraform/.build/`, `__pycache__/`); they create noisy diffs and are not source of truth.
- SFN ARN in the API handler Lambda env is a pre-computed string (`arn:aws:states:REGION:ACCOUNT:stateMachine:nexus-pipeline`) to break a Terraform circular dependency — if you rename the state machine, update `main.tf` accordingly.
- Lambda layers are built in `terraform/.build/layers/` by `deploy_tf.sh` using Docker for arm64 cross-compilation; they must exist before `terraform apply`.
- ECS images must be pushed to ECR before `terraform apply` — `deploy_tf.sh` handles this automatically.

## nexus-shorts Module

### Status
Implemented. `lambdas/nexus-shorts/` (handler + 15 modules + Dockerfile), ASL `ContentAssembly` Parallel branch, `docker-compose.yml` service, and Terraform compute resources (ECR repo, log group, ECS task def in `terraform/modules/compute/main.tf`) all exist.

### Purpose
Generates a batch of 4 vertical short-form MP4s (15s / 30s / 45s / 60s) from the same script and brand kit used for the long-form video. Outputs are ready for YouTube Shorts, Instagram Reels, and TikTok.

### File Layout
```
lambdas/nexus-shorts/
├── Dockerfile              ← python:3.12-slim + FFmpeg + shared utils copy
├── handler.py              ← Fargate entry, wires all modules
├── config.py               ← Tier defs, LUT presets, constants
├── section_scorer.py       ← Scores script sections 0–100 across 5 dimensions
├── script_condenser.py     ← Claude → short narration (30–160 words)
├── voiceover_generator.py  ← ElevenLabs TTS per channel voice_id
├── broll_fetcher.py        ← Nova Reel → Pexels → Nova Canvas → gradient
├── vertical_converter.py   ← Landscape → 1080×1920 (3 strategies)
├── motion_renderer.py      ← 7 overlay types as Pillow frame sequences
├── beat_syncer.py          ← librosa beat detection + cut snapping
├── clip_assembler.py       ← FFmpeg filter_complex assembly
├── loop_builder.py         ← Seamless loop + pixel similarity ≥ 85%
├── audio_mixer.py          ← VO + music mix, master to -14 LUFS
├── color_grader.py         ← LUT + vignette + sharpening
├── watermarker.py          ← Channel logo overlay (top center, 75% opacity)
├── batch_processor.py      ← ThreadPoolExecutor(max_workers=3) + retry
├── uploader.py             ← S3 multipart upload + manifest.json
└── requirements.txt        ← Must include librosa, Pillow, boto3, requests
```

### ASL Integration
The Editor step has been replaced with a `ContentAssembly` Parallel state in `statemachine/nexus_pipeline.asl.json`:
```
MergeParallelOutputs → ContentAssembly (Parallel)
  ├── Editor → SetEditorKeys   → long-form MP4
  └── CheckShortsEnabled → Shorts → SetShortsKeys (Catch → ShortsSkipped)
ContentAssembly → MergeContentOutputs → Thumbnail → Notify
```
- `CheckShortsEnabled` Choice state gates on `$.generate_shorts == true`; default routes to `ShortsSkipped`.
- Shorts branch `Catch` routes all errors to `ShortsSkipped` Pass state so main pipeline continues.
- `generate_shorts` and `shorts_tiers` are threaded through the full ASL state chain (present in all `ResultSelector`/`Parameters` from Research onward).
- Terraform orchestration module passes `NexusShortsTaskDefArn` via `templatefile()` (see `terraform/modules/orchestration/main.tf`).

### Duration Tiers
| Tier | Duration | Script Sections | Nova Reel Clips |
|------|----------|----------------|-----------------|
| `micro` | 15s | 1 | 2 |
| `short` | 30s | 2–3 | 4 |
| `mid` | 45s | 3–4 | 5 |
| `full` | 60s | 4–6 | 6 |

### Output Specs
- MP4 H.264+AAC, 1080×1920 (9:16), 30fps, CRF 18, AAC 192kbps, -14 LUFS, seamless loop, faststart.
- S3 layout: `{run_id}/shorts/short_{tier}_{num}.mp4` + `manifest.json` + `errors/{short_id}.json`.

### Key Design Decisions
- **All Nova Reel jobs submit in parallel at batch start** — by the time processing stages complete, results are ready.
- Nova Reel capped at `NOVA_REEL_SHORTS_BUDGET` (default 6); remaining slots fall through to Pexels.
- B-roll 4-tier fallback: Nova Reel → Pexels (portrait-first) → Nova Canvas + motion → brand gradient (never fails).
- Overlays rendered as Pillow PNG frame sequences (no libfreetype dependency), composited via FFmpeg `overlay`.
- Beat sync uses librosa with profile-specific BPM estimates (documentary 75, finance 95, entertainment 120); cuts snap to nearest beat ±0.4s with 3s minimum gap.
- Loop: render target + 1.5s, crossfade 0.5s at beat-aligned loop point, verify pixel similarity ≥ 85%.
- Individual short failures never stop the batch — `manifest.json` records per-short status.

### Environment Variables
```
SHORTS_ENABLED=true
SHORTS_TIERS=micro,short,mid,full
SHORTS_MAX_WORKERS=3
NOVA_REEL_SHORTS_BUDGET=6
SHORTS_LOOP_VERIFY=true
SHORTS_LOOP_THRESHOLD=0.85
SHORTS_OUTPUT_PREFIX=shorts/
```

### Docker Compose
Defined in `docker-compose.yml` — port `9014:8080`, `memory: 8g`, `cpus: 4`, volume `shorts_scratch:/mnt/scratch`, depends on `postgres` (healthy) + `setup-aws` (completed). Volume declared at top level alongside `pg_data`.

### IAM Permissions Required
Task role needs: `bedrock:InvokeModel`, `bedrock:StartAsyncInvoke`, `bedrock:GetAsyncInvoke`, `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `secretsmanager:GetSecretValue`.

### Conventions (follow existing patterns)
- Preserve state keys (`run_id`, `profile`, `dry_run`) — same as all other step handlers.
- Error pattern: log → write `s3://<outputs>/{run_id}/shorts/errors/{short_id}.json` → continue batch (do not raise for individual short failures).
- Cache Secrets Manager reads in module-level `_cache` dict.
- Use `notify_step_start` / `notify_step_complete` from `lambdas/nexus_pipeline_utils.py` for Discord notifications.
- Read brand kit (colors, font, voice_id, LUT) from profile JSON in `CONFIG_BUCKET`; LUT `.cube` files are in `ASSETS_BUCKET` (uploaded by `scripts/setup_luts.py`).
- ElevenLabs voice settings per profile are in profile JSON (`voice.stability`, `voice.similarity_boost`, `voice.style`), not hardcoded.

