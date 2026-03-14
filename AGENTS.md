# AGENTS.md — Nexus Cloud Pipeline

Read this fully before making any change to this repository.
This file is the single source of truth for AI coding agents.

---

## What this repo is

Nexus Cloud is a serverless YouTube automation pipeline running on AWS.
One API call produces a fully edited, uploaded YouTube video.

**Pipeline order (Step Functions):**
```
Research (Lambda)
  → Script (Lambda)
  → AudioVisuals (Parallel)
      ├── Audio (ECS Fargate)
      └── Visuals (ECS Fargate)
  → MergeParallelOutputs
  → ContentAssembly (Parallel)
      ├── Editor (ECS Fargate)
      └── CheckShortsEnabled → Shorts (ECS Fargate)  [optional, non-fatal]
  → MergeContentOutputs
  → Thumbnail (Lambda)
  → Notify (Lambda)
```

ASL starts with `Initialize → ResumeRouter` (Choice state).
`ResumeRouter` branches on `$.resume_from` to skip completed steps.
`_handle_resume` in each handler populates `resume_from` from S3 artifact detection.

---

## Pipeline modes (dashboard)

Three modes — each has its own step array, generate modal, cost calc, and visual identity:

| Mode | Field sent | Steps |
|---|---|---|
| `video` | `pipeline_type: "video", generate_shorts: false` | Research→Script→AudioVisuals→Editor→Thumbnail→Notify |
| `shorts` | `pipeline_type: "shorts", generate_shorts: true` | Research→Script→AudioVisuals→Shorts→Notify |
| `combined` | legacy `generate_shorts: true` | Full with ContentAssembly parallel |

`getPipelineType(runData)` infers type from `pipeline_type`, `generate_shorts`/`generate_video` flags,
or step name analysis.
`PIPELINE_TYPE_META` maps each type to icon, label, gradient, badge CSS, and description —
used in PipelineMonitor header, OutputsPanel, and sidebar active runs.
`GenerateVideoModal` sends `pipeline_type: 'video', generate_shorts: false`.
`GenerateShortsModal` sends `pipeline_type: 'shorts', generate_shorts: true`.

**Known bug — do not introduce regressions on this:**
`lambdas/nexus-api/handler.py` `_handle_run` does NOT currently forward
`generate_shorts`, `shorts_tiers`, or `channel_id` to the SFN execution input.
ASL references them via `$$.Execution.Input`. Shorts won't trigger unless
this handler is patched to forward these fields from the request body.

---

## AWS region and confirmed services

Region: `us-east-1`

All services below are confirmed active on this account:

| Service | Used for |
|---|---|
| AWS Bedrock | LLM calls (Claude models only) |
| Amazon Polly Neural | Primary TTS (Gregory / Matthew / Stephen) |
| Amazon Transcribe | Word-level timestamps from audio |
| Amazon Rekognition | B-roll image scoring |
| Amazon SQS | Upload job queue + DLQ |
| Amazon SNS | Notification fan-out |
| Amazon DynamoDB | Run log storage |
| AWS X-Ray | ECS task tracing |
| AWS MediaConvert | Video transcoding |
| AWS Step Functions | Pipeline orchestration |
| AWS ECS Fargate | Audio, Visuals, Editor, Shorts tasks |
| AWS Secrets Manager | All API keys (never env vars at runtime) |
| Amazon S3 | Assets, outputs, config (3 buckets) |
| Amazon EFS | Shared scratch space at /mnt/scratch |

---

## Bedrock models — use these exact IDs

```python
# Research, Script passes 1-5, Visuals scoring, Thumbnail
SONNET = "anthropic.claude-sonnet-4-5-20250929-v1:0"

# Script pass 6 (final polish) ONLY
OPUS   = "anthropic.claude-opus-4-5-20251101-v1:0"
```

Never use or introduce:
- `anthropic.claude-3-5-sonnet-20241022-v2:0` — outdated
- `anthropic.claude-3-sonnet-20240229-v1:0` — outdated
- Any NVIDIA NIM endpoint — removed from this project
- Any OpenAI endpoint — not used in this project
- `amazon.nova-2-sonic-v1:0` or `amazon.nova-sonic-v1:0` — speech-input conversational
  models, NOT text-to-speech. Do not use for TTS under any circumstances.

---

## TTS provider hierarchy — always implement as 3-tier cascade

Never call a single TTS provider directly. Always use this cascade:

```
Tier 1: ElevenLabs        — best quality, use when quota available
Tier 2: Polly Neural      — SSML emotion mapping, engine="neural", OutputFormat="mp3"
Tier 3: Polly Standard    — guaranteed fallback, always succeeds, no SSML
```

Trigger fallback to Tier 2 on ElevenLabs: HTTP 401, HTTP 429,
or response body containing `"quota_exceeded"` or `"credits_used"`.
Trigger fallback to Tier 3 if Tier 2 raises any exception.
The cascade function must never raise unless all 3 tiers fail.

**SSML emotion mapping for Tier 2:**
```python
EMOTION_SSML = {
    "tense":         {"rate": "slow",   "pitch": "-2st"},
    "excited":       {"rate": "fast",   "pitch": "+3st"},
    "reflective":    {"rate": "x-slow", "pitch": "-3st"},
    "authoritative": {"rate": "medium", "pitch": "-1st"},
    "somber":        {"rate": "slow",   "pitch": "-4st"},
    "hopeful":       {"rate": "medium", "pitch": "+1st"},
    "neutral":       {"rate": "medium", "pitch": "0st"},
}
```

SSML wrapper:
```xml
<speak>
  <prosody rate="{rate}" pitch="{pitch}">
    <amazon:effect name="drc">{text}</amazon:effect>
  </prosody>
</speak>
```

**Polly voice per profile** — read `polly_voice_id` from profile dict first,
use this map as fallback:
```python
POLLY_VOICE_DEFAULTS = {
    "documentary":   "Gregory",
    "finance":       "Matthew",
    "entertainment": "Stephen",
}
```

---

## Secret fetching — strict rules

All secrets come from AWS Secrets Manager. Never from `os.environ` at runtime.

Every Lambda and ECS handler uses this exact pattern:
```python
_cache = {}

def _get_secret(key: str) -> str:
    if key not in _cache:
        client = boto3.client('secretsmanager')
        _cache[key] = json.loads(
            client.get_secret_value(SecretId=key)['SecretString']
        )
    return _cache[key]
```

Canonical secret names (defined in `terraform/modules/secrets/`):
- `nexus/elevenlabs_api_key`  → key: `api_key`
- `nexus/perplexity_api_key`  → key: `api_key`
- `nexus/pexels_api_key`      → key: `api_key`
- `nexus/pixabay_api_key`     → key: `api_key`
- `nexus/freesound_api_key`   → key: `api_key`
- `nexus/youtube_credentials` → keys: `client_id`, `client_secret`, `refresh_token`, `access_token`
- `nexus/db_credentials`      → keys: `host`, `port`, `dbname`, `user`, `password`
- `nexus/api_key`             → key: `key`

Do not add new secrets outside the `nexus/` prefix.
Do not read secrets from `os.environ` in any Lambda or ECS handler.

---

## Infrastructure — deploy rules

**Primary deploy path: Terraform only.**
```
terraform/
  modules/
    storage/       ← S3 buckets (imports pre-existing + dashboard bucket + profile uploads)
    secrets/       ← Secrets Manager (all nexus/* secrets)
    networking/    ← Default VPC lookup, EFS file system + access point, NFS SG
    identity/      ← ALL IAM roles — edit here for permissions
    compute/       ← Lambda zips, ECS cluster, ECR repos, Fargate task definitions
    orchestration/ ← Step Functions state machine via templatefile()
    api/           ← API Gateway REST (5 routes + CORS) + CloudFront
    observability/ ← EventBridge schedule (disabled), CloudWatch dashboard
```

**Never run `terraform apply` directly.** Always:
```bash
bash terraform/scripts/deploy_tf.sh
```

This script: reads `.env` → copies shared utils → builds Lambda layers (arm64 via Docker)
→ builds + pushes ECS images to ECR → generates `terraform.tfvars` → runs `terraform apply`.

**Legacy CDK (`infrastructure/nexus_stack.py`) is frozen — never modify it.**

**Shared utils rule:**
`deploy_tf.sh` copies `lambdas/nexus_pipeline_utils.py` into every `lambdas/nexus-*/` folder.
Always edit the root shared file — never the per-lambda copies.

**IAM rule:**
When adding any new AWS service call, always add the IAM action to the correct role
in `terraform/modules/identity/main.tf` in the same commit.

Roles to edit:
- **ECS task role** — used by nexus-audio, nexus-visuals, nexus-editor, nexus-shorts
- **Lambda execution role** — used by all Lambda functions
- **Step Functions role** — uses wildcard `arn:aws:lambda:REGION:ACCOUNT:function:nexus-*`

**SFN ARN note:**
The SFN ARN in the API handler Lambda env is pre-computed to break a Terraform circular dependency.
If you rename the state machine, update `main.tf` accordingly.

---

## State machine rules

File: `statemachine/nexus_pipeline.asl.json`
Wired by `terraform/modules/orchestration/` via `templatefile()` for ARN injection.

**Never change:**
- State names
- Parallel branch structure (AudioVisuals, ContentAssembly, MergeParallelOutputs, MergeContentOutputs)
- The step order
- `generate_shorts` and `shorts_tiers` threading through `ResultSelector`/`Parameters`
  from Research onward — Shorts gating depends on these fields being present everywhere

**You may change:**
- `Resource` ARN of a state (e.g. Lambda → SQS → SNS)
- `Parameters` block of a state
- `.catch` and `.retry` blocks

**ASL integration patterns:**
- `CheckShortsEnabled` Choice state gates on `$.generate_shorts == true`
- Shorts branch `Catch` routes all errors to `ShortsSkipped` Pass state — never blocks main pipeline
- All new AWS service integrations must use `arn:aws:states:::` SDK integration prefix

**Known mismatch — do not fix unless explicitly asked:**
`lambdas/nexus-api/handler.py` `PIPELINE_STEPS` is a flat list used for status parsing only.
It does not reflect ASL parallel states. Step history parsing may miss parallel branch events.

---

## ECS Fargate task rules

Each ECS service: `lambdas/nexus-{service}/Dockerfile`
Shared media helpers: `lambdas/shared/` (`nova_canvas.py`, `nova_reel.py`, `motion.py`)
Fargate Dockerfiles copy shared helpers into the image at build time.

- Shared scratch: `/mnt/scratch` (EFS mount)
- Write intermediates to `/mnt/scratch/{run_id}/`
- Final outputs always go to S3 — never leave files on EFS after a run
- Always clean up `/mnt/scratch/{run_id}/` on completion
- Memory-intensive operations (FFmpeg, MediaConvert polling) must have timeouts
- All steps must be resumable — check S3 for existing output before reprocessing

X-Ray tracing required on all ECS tasks:
```python
from aws_xray_sdk.core import xray_recorder, patch_all
patch_all()
```

API lambda injects ECS subnet IDs via `ECS_SUBNETS` env var.
ASL ECS tasks consume `$.subnets` for `AwsvpcConfiguration`.

---

## nexus-shorts module

### File layout
```
lambdas/nexus-shorts/
├── Dockerfile
├── handler.py
├── config.py               ← tier defs, LUT presets, constants
├── section_scorer.py       ← scores script sections 0-100 across 5 dimensions
├── script_condenser.py     ← Claude → short narration (30-160 words)
├── voiceover_generator.py  ← TTS per channel voice_id (3-tier cascade required)
├── broll_fetcher.py        ← Nova Reel → Pexels → Nova Canvas → gradient
├── vertical_converter.py   ← landscape → 1080×1920 (3 strategies)
├── motion_renderer.py      ← 7 overlay types as Pillow frame sequences
├── beat_syncer.py          ← librosa beat detection + cut snapping
├── clip_assembler.py       ← FFmpeg filter_complex assembly
├── loop_builder.py         ← seamless loop + pixel similarity ≥ 85%
├── audio_mixer.py          ← VO + music mix, master to -14 LUFS
├── color_grader.py         ← LUT + vignette + sharpening
├── watermarker.py          ← channel logo overlay (top center, 75% opacity)
├── batch_processor.py      ← ThreadPoolExecutor(max_workers=3) + retry
├── uploader.py             ← S3 multipart upload + manifest.json
└── requirements.txt        ← must include librosa, Pillow, boto3, requests
```

### Duration tiers
| Tier | Duration | Nova Reel clips |
|---|---|---|
| `micro` | 15s | 2 |
| `short` | 30s | 4 |
| `mid`   | 45s | 5 |
| `full`  | 60s | 6 |

### Output specs
MP4 H.264+AAC, 1080×1920 (9:16), 30fps, CRF 18, AAC 192kbps, −14 LUFS, seamless loop, faststart.
S3: `{run_id}/shorts/short_{tier}_{n}.mp4` + `manifest.json` + `errors/{short_id}.json`

### Key design decisions — do not break these
- All Nova Reel jobs submit in parallel at batch start
- Nova Reel capped at `NOVA_REEL_SHORTS_BUDGET` (default 6); remaining slots fall to Pexels
- B-roll 4-tier fallback: Nova Reel → Pexels (portrait-first) → Nova Canvas + motion → brand gradient
- Beat sync uses profile-specific BPM: documentary 75, finance 95, entertainment 120
- Cuts snap to nearest beat ±0.4s with 3s minimum gap
- Loop: render target + 1.5s, crossfade 0.5s at beat-aligned point, verify pixel similarity ≥ 85%
- Individual short failures never stop the batch — `manifest.json` records per-short status
- Overlays rendered as Pillow PNG frame sequences (no libfreetype dependency)

### Transcribe timestamp reuse
Before synthesizing new audio in `voiceover_generator.py`, check if
`s3://nexus-outputs/{run_id}/audio/word_timestamps.json` exists.
If it does, read and reuse those timestamps — do not re-synthesize.

### Docker Compose
Port `9014:8080`, `memory: 8g`, `cpus: 4`, volume `shorts_scratch:/mnt/scratch`,
depends on `postgres` (healthy) + `setup-aws` (completed).

### IAM permissions required (ECS task role)
`bedrock:InvokeModel`, `bedrock:StartAsyncInvoke`, `bedrock:GetAsyncInvoke`,
`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `secretsmanager:GetSecretValue`,
`polly:SynthesizeSpeech`, `transcribe:GetTranscriptionJob`

---

## S3 bucket conventions

Three buckets — never cross-write:
- `nexus-assets`  → source media (stock footage, music, SFX, LUT .cube files)
- `nexus-outputs` → all run artifacts (`{run_id}/` prefix always)
- `nexus-config`  → profile JSONs, channel configs

Key pattern for run artifacts:
```
{run_id}/research.json
{run_id}/script.json
{run_id}/audio/mixed_audio.wav
{run_id}/audio/word_timestamps.json
{run_id}/visuals/{clip_id}.mp4
{run_id}/editor/final.mp4
{run_id}/shorts/short_{tier}_{n}.mp4
{run_id}/shorts/manifest.json
{run_id}/thumbnails/thumb_{n}.jpg
{run_id}/errors/{step}.json
```

---

## Profile JSON schema

Profiles live in `profiles/`, uploaded to `nexus-config` S3 by `deploy_tf.sh`.

Required keys — never remove existing keys, only add:
```json
{
  "voice": {
    "voice_id": "...",
    "stability": 0.35,
    "similarity_boost": 0.75,
    "style": 0.45
  },
  "polly_voice_id": "Gregory",
  "llm": { "script_model": "..." },
  "script": { "target_duration_min": 10, "target_duration_max": 16 },
  "editing": { "cuts_per_minute_target": 4 },
  "visuals": { "color_grade_default": "cinematic_warm" }
}
```

Brand kit (colors, font, voice_id, LUT) is read from profile JSON in `CONFIG_BUCKET`.
LUT `.cube` files are in `ASSETS_BUCKET` (uploaded by `scripts/setup_luts.py`).
ElevenLabs voice settings per profile: `voice.stability`, `voice.similarity_boost`, `voice.style`
— never hardcode these values.

---

## Error handling rules

- Every Lambda and ECS handler wraps its logic in `try/except`
- Errors write to `s3://nexus-outputs/{run_id}/errors/{step}.json`, then raise
- LLM calls retry up to 3× with exponential backoff: `time.sleep(2 ** attempt)`
- Notifications are first-class: use `notify_step_start` / `notify_step_complete`
  from `lambdas/nexus_pipeline_utils.py` for Discord messages

Non-fatal steps — log warning and continue, never raise:
- Transcribe word timestamps
- Shorts individual tiers (write to `shorts/errors/{short_id}.json`, continue batch)
- `nexus-intro-outro` (stub, non-fatal in channel setup)

---

## Testing rules

```bash
python3 -m pytest scripts/tests/ -q --tb=short          # full suite
python3 -m pytest scripts/tests/ -m unit -v             # unit only
RUN_AWS_TESTS=1 python3 -m pytest scripts/tests/ -v     # with AWS integration
python3 -m pytest scripts/test_check_external.py -v     # fast loop for external-API check logic
```

**All AWS calls must be mocked in unit tests.** Use `unittest.mock.patch` or `pytest-mock`.
No test may make a live AWS call. No test may read from `.env`.

Minimum new tests per feature:
- Happy path (success)
- Primary failure → fallback triggered
- All fallbacks exhausted → correct error raised or non-fatal continue

Test markers:
- `@pytest.mark.unit` — default for all new tests
- `@pytest.mark.regression` — for previously broken behaviour
- `@pytest.mark.smoke` — requires `NEXUS_API_URL` env var, post-deploy only

---

## Code style

- Python 3.12+
- `boto3` clients initialised at module level (outside handler function) for connection reuse
- `logger = logging.getLogger(__name__)` — no `print()` statements in handlers
- Log format: `logger.info(f"[{run_id}] step: message")`
- State keys `run_id`, `profile`, `dry_run` must be preserved in every handler output —
  ASL `ResultSelector`/`Pass` states expect exact field names
- Line length: 100 characters max

---

## Channel management

Channel CRUD routes (`/channel/create`, `/channel/list`, `/channel/{id}`, etc.)
are called by the dashboard but are **not yet implemented** in `lambdas/nexus-api/handler.py`.

Implemented channel Lambdas:
- `nexus-channel-setup` — orchestrates brand → logo → intro/outro
- `nexus-brand-designer` — Claude brand kit generation
- `nexus-logo-gen` — Nova Canvas logo + Pillow fallback

Not yet implemented:
- `nexus-intro-outro` — stub only, no `handler.py`

---

## Gotchas — read before touching anything

- **Do not edit generated artifacts**: `infrastructure/cdk.out/`, `terraform/.build/`, `__pycache__/`
- **Lambda layers** built in `terraform/.build/layers/` by `deploy_tf.sh` via Docker arm64 — must exist before `terraform apply`
- **ECS images** must be pushed to ECR before `terraform apply` — `deploy_tf.sh` handles this
- **Cloud vs local mismatch**: ASL routes `Thumbnail → Notify` (no Upload task); `scripts/orchestrator.py` still includes Upload — both run Editor ∥ Shorts in parallel
- **README.md** references `test_repair.py` and `test_drawtext.py` which do not exist — current tests are under `scripts/tests/`
- **API entrypoint**: `lambdas/nexus-api/handler.py` handles `/run`, `/resume`, `/status/{run_id}`, `/outputs/{run_id}`, `/health`
- **Dashboard** is a single-file React 18 app at `dashboard/index.html`, locally served by `scripts/orchestrator.py` on port 3000, deployed to CloudFront via `terraform/modules/api/`

---

## What NOT to do

- Do not use `os.environ` for secrets in Lambda/ECS handlers
- Do not modify `infrastructure/` (legacy CDK — frozen)
- Do not run `terraform apply` directly — always use `deploy_tf.sh`
- Do not add Python dependencies without updating the relevant `requirements.txt`
- Do not change state machine step names or parallel branch structure
- Do not introduce `openai`, `nvidia`, or `nim` client libraries
- Do not use Nova Sonic models for TTS — wrong input modality
- Do not hardcode voice settings — always read from profile dict
- Do not leave files on EFS after a run — clean up `/mnt/scratch/{run_id}/`
- Do not edit per-lambda copies of `nexus_pipeline_utils.py` — edit the root file only