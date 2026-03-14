# Nexus Pipeline — Copilot CLI Task Prompts

One prompt per session. Paste each block exactly as written into Copilot CLI or Copilot Workspace.
Run them in order — each task has dependencies on the previous.

---

## SESSION 1 — Audio: Polly fallback cascade

```
In `lambdas/nexus-audio/handler.py`:

1. Wrap the existing ElevenLabs TTS call in a 3-tier cascade function:
   - Tier 1: existing ElevenLabs call (unchanged)
   - Tier 2: Amazon Polly Neural via boto3 with SSML prosody
   - Tier 3: Amazon Polly Standard via boto3 (no SSML)

   Trigger fallback to Tier 2 on: HTTP 401, HTTP 429, or response body containing "quota_exceeded" or "credits_used".
   Trigger fallback to Tier 3 if Tier 2 raises any exception.
   The cascade must never raise — always return audio bytes or raise only if all 3 tiers fail.

2. Add SSML emotion mapping for Tier 2:
   - tense:         rate=slow,   pitch=-2st
   - excited:       rate=fast,   pitch=+3st
   - reflective:    rate=x-slow, pitch=-3st
   - authoritative: rate=medium, pitch=-1st
   - somber:        rate=slow,   pitch=-4st
   - hopeful:       rate=medium, pitch=+1st
   - neutral:       rate=medium, pitch=0st

   Wrap text in:
   <speak><prosody rate="{rate}" pitch="{pitch}"><amazon:effect name="drc">{text}</amazon:effect></prosody></speak>

3. Polly voice selection: read "polly_voice_id" from the profile dict.
   Fallback map if key missing: documentary=Gregory, finance=Matthew, entertainment=Stephen.
   Always use engine="neural" for Tier 2, OutputFormat="mp3".

4. Apply the exact same cascade to `lambdas/nexus-shorts/voiceover_generator.py`.

5. In `scripts/tests/test_audio_handler.py` add these 4 tests (mock all AWS calls):
   - test_elevenlabs_429_triggers_polly_fallback
   - test_elevenlabs_401_triggers_polly_fallback
   - test_polly_neural_ssml_emotion_mapping (assert each emotion produces correct rate/pitch)
   - test_polly_standard_called_when_neural_fails
```

---

## SESSION 2 — Audio: Transcribe word-level timestamps

```
In `lambdas/nexus-audio/handler.py`:

After the final mixed_audio.wav is written to S3, submit it to Amazon Transcribe
and save word-level timestamps back to S3.

1. Call `transcribe.start_transcription_job` with:
   - TranscriptionJobName: f"nexus-{run_id}"
   - MediaFormat: "wav"
   - Media: {"MediaFileUri": s3_uri_of_mixed_audio}
   - OutputBucketName: the nexus-outputs bucket
   - OutputKey: f"{run_id}/audio/transcribe_timestamps.json"
   - Settings: {"ShowWordConfidence": True}

2. Poll `transcribe.get_transcription_job` every 5 seconds, max 120 seconds.
   If status is COMPLETED, parse the output file from S3 and write a simplified
   JSON file to s3://nexus-outputs/{run_id}/audio/word_timestamps.json with format:
   {"words": [{"word": str, "start": float, "end": float, "confidence": float}]}

3. On timeout or any exception: log a warning and continue. This step is non-fatal.

4. In `lambdas/nexus-shorts/voiceover_generator.py`:
   Before synthesizing new audio, check if
   s3://nexus-outputs/{run_id}/audio/word_timestamps.json exists.
   If it does, read and reuse those timestamps instead of re-synthesizing.

5. Add 2 tests in `scripts/tests/test_audio_handler.py` (mock all AWS calls):
   - test_transcribe_timestamps_written_to_s3
   - test_transcribe_timeout_is_nonfatal (mock timeout → assert pipeline continues)
```

---

## SESSION 3 — Script: Model routing per pass + prompt caching

```
In `lambdas/nexus-script/handler.py`:

1. Replace the single Bedrock model ID used for all script passes with per-pass routing:
   - Passes 1-5: "anthropic.claude-sonnet-4-5-20250929-v1:0"
   - Pass 6 (final polish): "anthropic.claude-opus-4-5-20251101-v1:0"
   
   The Perplexity fact-check call (also called pass 6 in some comments) is unchanged —
   only the Bedrock passes are affected.

2. For passes 1-5, add Bedrock prompt caching to the system prompt.
   In the converse() call, change the system parameter to:
   [{"type": "text", "text": <existing_system_prompt>, "cache_control": {"type": "ephemeral"}}]
   
   Do not add cache_control to pass 6 — Opus is called once and caching has no benefit.

3. Add 3 tests in `scripts/tests/test_script_handler.py` (mock Bedrock calls):
   - test_pass_6_uses_opus_model (assert modelId contains "opus-4-5")
   - test_passes_1_to_5_use_sonnet_model (assert modelId contains "sonnet-4-5")
   - test_system_prompt_has_cache_control (assert cache_control key present in system block for pass 1)
```

---

## SESSION 4 — Research: Model upgrade

```
In `lambdas/nexus-research/handler.py`:

Replace the Bedrock model ID used for topic selection with:
"anthropic.claude-sonnet-4-5-20250929-v1:0"

Find every hardcoded reference to the old model ID
("anthropic.claude-3-5-sonnet-20241022-v2:0" or similar) and replace it.
Do not change the Perplexity sonar-pro call — only the Bedrock call.

Add 1 test in `scripts/tests/test_research_handler.py`:
- test_research_uses_sonnet_4_5_model
  Assert the Bedrock converse() call receives modelId="anthropic.claude-sonnet-4-5-20250929-v1:0"
```

---

## SESSION 5 — Visuals: Replace CLIP with Rekognition + Claude vision

```
In `lambdas/nexus-visuals/handler.py` (or whichever file contains the b-roll scoring logic):

1. Remove all CLIP imports, model loading, and local inference code.

2. Replace the CLIP scoring function with a 2-step AWS-native pipeline:

   Step 1 — Rekognition (run on ALL candidates, fast):
   def _rekognition_score(image_bytes: bytes, visual_cue: str) -> float:
       response = rekognition.detect_labels(Image={"Bytes": image_bytes}, MaxLabels=20, MinConfidence=50)
       labels = {l["Name"].lower() for l in response["Labels"]}
       cue_words = set(visual_cue.lower().split())
       return len(labels & cue_words) / max(len(cue_words), 1)

   Step 2 — Claude vision (run on TOP 3 only, accurate):
   def _claude_vision_score(image_bytes: bytes, visual_cue: str) -> float:
       response = bedrock.converse(
           modelId="anthropic.claude-sonnet-4-5-20250929-v1:0",
           messages=[{"role": "user", "content": [
               {"image": {"format": "jpeg", "source": {"bytes": image_bytes}}},
               {"text": f"Score 0.0 to 1.0 how well this image matches: '{visual_cue}'. Reply with only the number."}
           ]}]
       )
       return float(response["output"]["message"]["content"][0]["text"].strip())

   Combined: score all candidates with Rekognition, take top 3, score those with Claude vision,
   return the highest-scoring candidate.

3. Do not remove Nova Reel or Nova Canvas — only the scoring/selection logic changes.
```

---

## SESSION 6 — Thumbnail: Replace NVIDIA NIM with Bedrock Claude vision

```
In `lambdas/nexus-thumbnail/handler.py`:

1. Replace the NVIDIA NIM frame scoring call (microsoft/phi-3.5-vision-instruct) with:

   def _score_frame(frame_bytes: bytes, topic: str) -> float:
       response = bedrock.converse(
           modelId="anthropic.claude-sonnet-4-5-20250929-v1:0",
           messages=[{"role": "user", "content": [
               {"image": {"format": "jpeg", "source": {"bytes": frame_bytes}}},
               {"text": f"Rate this frame 0.0-10.0 as a YouTube thumbnail for the topic: '{topic}'. Consider visual clarity, emotional impact, faces/eyes if present. Reply with only the number."}
           ]}]
       )
       return float(response["output"]["message"]["content"][0]["text"].strip())

2. Replace the NVIDIA NIM concept generation call (meta/llama-3.1-70b-instruct) with:

   def _generate_thumbnail_concepts(script_summary: str, topic: str) -> list:
       response = bedrock.converse(
           modelId="anthropic.claude-sonnet-4-5-20250929-v1:0",
           system=[{"text": "You generate YouTube thumbnail concepts. Return valid JSON only. No markdown, no explanation."}],
           messages=[{"role": "user", "content": [{"text": f"Generate 3 thumbnail concepts for: '{topic}'. Summary: {script_summary[:500]}. Return JSON array: [{{'title': str, 'overlay_text': str, 'mood': str, 'nova_canvas_prompt': str}}]"}]}]
       )
       import json
       return json.loads(response["output"]["message"]["content"][0]["text"].strip())

3. Remove any import or client initialisation used exclusively for NVIDIA NIM
   (check for openai, nvidia, nim in imports). Do not remove if used elsewhere.

4. Keep Nova Canvas thumbnail compositing unchanged.
```

---

## SESSION 7 — Editor: MediaConvert YouTube preset

```
In `lambdas/nexus-editor/handler.py`:

Find the MediaConvert job submission (create_job call).
Replace or update the output group settings with a YouTube-optimised preset:

Video:
- Codec: H_264, Profile: HIGH, Level: LEVEL_4_1
- RateControlMode: CBR, Bitrate: 8000000, MaxBitrate: 10000000
- GopSize: 2.0, GopSizeUnits: SECONDS
- NumberBFramesBetweenReferenceFrames: 2
- QualityTuningLevel: MULTI_PASS_HQ
- SceneChangeDetect: ENABLED
- Width: 1920, Height: 1080

Audio:
- Codec: AAC, Bitrate: 320000, SampleRate: 48000
- CodingMode: CODING_MODE_2_0
- AudioNormalizationSettings:
    Algorithm: ITU_BS_1770_3
    AlgorithmControl: CORRECT_AUDIO
    TargetLkfs: -14.0

Container: MP4, Extension: mp4

Do not change any other part of the job settings (input files, S3 paths, role ARN).
```

---

## SESSION 8 — Editor: X-Ray tracing

```
In `lambdas/nexus-editor/handler.py`:

1. Add at the top of the file:
   from aws_xray_sdk.core import xray_recorder, patch_all
   patch_all()

2. Wrap the MediaConvert job submission in an X-Ray subsegment:
   with xray_recorder.in_subsegment("mediaconvert-submit"):
       job = mc_client.create_job(...)

3. Wrap each S3 upload in an X-Ray subsegment:
   with xray_recorder.in_subsegment("s3-upload"):
       s3_client.upload_file(...)

4. Add "aws-xray-sdk" to `lambdas/nexus-editor/requirements.txt` if not already present.

5. In `terraform/modules/compute/main.tf`, find the nexus-editor ECS task definition.
   Add an X-Ray daemon sidecar container to its container_definitions:
   {
     "name": "xray-daemon",
     "image": "amazon/aws-xray-daemon",
     "essential": false,
     "portMappings": [{"containerPort": 2000, "protocol": "udp"}],
     "cpu": 32,
     "memory": 256
   }

6. In `terraform/modules/identity/main.tf`, add to the ECS task role policy:
   "xray:PutTraceSegments",
   "xray:PutTelemetryRecords"
```

---

## SESSION 9 — Upload: SQS decoupling

```
In `terraform/modules/compute/main.tf` (or create `terraform/modules/compute/upload_queue.tf`):

1. Add two SQS resources:
   - Queue: name="nexus-upload-queue", visibility_timeout=900,
     message_retention=86400, receive_wait_time=20 (long polling),
     redrive_policy pointing to the DLQ with maxReceiveCount=3
   - DLQ: name="nexus-upload-dlq", message_retention=1209600

2. In `statemachine/nexus_pipeline.asl.json`:
   Change the Upload state Resource from the Lambda ARN to:
   "arn:aws:states:::sqs:sendMessage.waitForTaskToken"
   
   Change Parameters to:
   {
     "QueueUrl": "${UploadQueueUrl}",
     "MessageBody": {
       "run_id.$": "$.run_id",
       "s3_key.$": "$.video_s3_key",
       "metadata.$": "$.video_metadata",
       "task_token.$": "$$.Task.Token"
     }
   }
   Add "HeartbeatSeconds": 3600 to the state.

3. In `lambdas/nexus-upload/handler.py`:
   Change the handler to read from SQS event Records instead of direct invocation:
   - Loop over event["Records"]
   - Parse body = json.loads(record["body"])
   - Extract run_id and task_token from body
   - On success: call sfn.send_task_success(taskToken=task_token, output=json.dumps(result))
   - On failure: call sfn.send_task_failure(taskToken=task_token, error=type(e).__name__, cause=str(e))

4. In `terraform/modules/identity/main.tf`, add to the Lambda execution role:
   "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
   "sqs:SendMessage", "states:SendTaskSuccess", "states:SendTaskFailure"

5. In `terraform/outputs.tf` add:
   output "upload_queue_url" { value = aws_sqs_queue.nexus_upload_queue.url }
   output "upload_dlq_url"   { value = aws_sqs_queue.nexus_upload_dlq.url }
```

---

## SESSION 10 — Notify: SNS fan-out + DynamoDB run logs

```
In `terraform/modules/compute/main.tf` (or new file `terraform/modules/compute/notify_infra.tf`):

1. Add SNS topic:
   resource "aws_sns_topic" "nexus_notifications" { name = "nexus-pipeline-notifications" }
   
   Add Lambda permission for SNS to invoke nexus-notify:
   resource "aws_lambda_permission" "sns_notify" {
     action        = "lambda:InvokeFunction"
     function_name = aws_lambda_function.nexus_notify.function_name
     principal     = "sns.amazonaws.com"
     source_arn    = aws_sns_topic.nexus_notifications.arn
   }
   
   Add SNS subscription:
   resource "aws_sns_topic_subscription" "discord" {
     topic_arn = aws_sns_topic.nexus_notifications.arn
     protocol  = "lambda"
     endpoint  = aws_lambda_function.nexus_notify.arn
   }

2. Add DynamoDB table:
   resource "aws_dynamodb_table" "nexus_run_logs" {
     name         = "nexus-run-logs"
     billing_mode = "PAY_PER_REQUEST"
     hash_key     = "run_id"
     range_key    = "timestamp"
     attribute { name = "run_id"    type = "S" }
     attribute { name = "timestamp" type = "S" }
     ttl { attribute_name = "ttl" enabled = true }
   }

3. In `lambdas/nexus-notify/handler.py`:
   Add a _write_run_log function that writes to DynamoDB before sending Discord:
   - Item must include: run_id, timestamp (ISO format), status, ttl (now + 90 days as epoch int)
   - Wrap the DynamoDB write in try/except — log warning on failure, never raise
   - Discord webhook call is unchanged and happens after the DynamoDB write attempt

4. In `statemachine/nexus_pipeline.asl.json`:
   Change terminal Notify states to publish to SNS instead of directly invoking Lambda:
   Resource: "arn:aws:states:::sns:publish"
   Parameters: { "TopicArn": "${NotificationTopicArn}", "Message.$": "States.JsonToString($)" }

5. In `terraform/modules/identity/main.tf`, add to Lambda execution role:
   "sns:Publish", "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"

6. In `terraform/outputs.tf` add:
   output "notification_topic_arn" { value = aws_sns_topic.nexus_notifications.arn }
   output "run_logs_table_name"    { value = aws_dynamodb_table.nexus_run_logs.name }
```

---

## SESSION 11 — Profiles + final IAM audit

```
1. In `profiles/documentary.json`:
   Add key: "polly_voice_id": "Gregory"

2. In `profiles/finance.json`:
   Add key: "polly_voice_id": "Matthew"

3. In `profiles/entertainment.json`:
   Add key: "polly_voice_id": "Stephen"

4. In `terraform/modules/identity/main.tf`:
   Audit the ECS task role (used by nexus-audio, nexus-visuals, nexus-editor, nexus-shorts).
   Confirm ALL of these permissions are present (add any missing):
   "polly:SynthesizeSpeech",
   "transcribe:StartTranscriptionJob",
   "transcribe:GetTranscriptionJob",
   "rekognition:DetectLabels",
   "rekognition:DetectText",
   "xray:PutTraceSegments",
   "xray:PutTelemetryRecords",
   "s3:GetObject",
   "s3:PutObject"

   Audit the Lambda execution role (used by nexus-research, nexus-script, nexus-thumbnail,
   nexus-upload, nexus-notify). Confirm ALL of these permissions are present:
   "bedrock:InvokeModel",
   "sqs:ReceiveMessage",
   "sqs:DeleteMessage",
   "sqs:GetQueueAttributes",
   "sqs:SendMessage",
   "sns:Publish",
   "dynamodb:PutItem",
   "dynamodb:GetItem",
   "dynamodb:Query",
   "states:SendTaskSuccess",
   "states:SendTaskFailure"

   Audit the Step Functions execution role. Confirm:
   "sns:Publish",
   "sqs:SendMessage",
   "lambda:InvokeFunction"

5. Run the full test suite:
   python3 -m pytest scripts/tests/ -q --tb=short
   All tests must pass before deploy.
```

---

## Deploy (run in your terminal after all sessions complete)

```bash
bash terraform/scripts/deploy_tf.sh

cd terraform && bash scripts/validate_deploy.sh

curl -X POST https://<your-api-url>/run \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: your-api-key' \
  -d '{"niche":"technology","profile":"documentary","dry_run":true}'
```

---

## Quick reference — session order and priority

| Session | Task | Priority |
|---|---|---|
| 1 | Audio Polly cascade | CRITICAL — fixes broken pipeline |
| 2 | Audio Transcribe timestamps | High |
| 3 | Script model routing + caching | High — biggest cost saving |
| 4 | Research model upgrade | Easy win |
| 5 | Visuals CLIP → Rekognition | Medium |
| 6 | Thumbnail NIM → Bedrock | High — removes 2 external APIs |
| 7 | Editor MediaConvert preset | Medium |
| 8 | Editor X-Ray tracing | Medium |
| 9 | Upload SQS decoupling | Medium |
| 10 | Notify SNS + DynamoDB | Medium |
| 11 | Profiles + IAM audit | Required before deploy |