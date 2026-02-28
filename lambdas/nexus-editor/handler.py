import json
import math
import os
import subprocess
import tempfile
import time
import boto3

MEDIACONVERT_THRESHOLD_SECONDS = 600

_cache: dict = {}


def get_secret(name: str) -> dict:
    if name not in _cache:
        client = boto3.client("secretsmanager")
        _cache[name] = json.loads(
            client.get_secret_value(SecretId=name)["SecretString"]
        )
    return _cache[name]


S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")
FFMPEG_BIN = "/opt/bin/ffmpeg"
FFPROBE_BIN = "/opt/bin/ffprobe"


def _detect_beats(audio_path: str) -> list[float]:
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        return beat_times
    except Exception:
        return []


def _snap_to_beat(timestamp: float, beats: list[float], window: float = 0.4) -> float:
    if not beats:
        return timestamp
    closest = min(beats, key=lambda b: abs(b - timestamp))
    if abs(closest - timestamp) <= window:
        return closest
    return timestamp


def _get_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, check=True,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 5.0


def _build_intro_slate(
    channel_name: str,
    video_title: str,
    tmpdir: str,
    accent_color: str = "#C8A96E",
) -> str:
    out = os.path.join(tmpdir, "intro_slate.mp4")
    title_escaped = video_title.replace("'", "\\'").replace(":", "\\:")
    channel_escaped = channel_name.replace("'", "\\'")
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi",
        "-i", "color=c=black:size=1920x1080:duration=5:rate=25",
        "-vf", (
            f"drawtext=text='{channel_escaped}'"
            f":fontcolor={accent_color}:fontsize=48:x=(w-text_w)/2:y=h/2-80"
            f":alpha='if(lt(t,0.5),0,if(lt(t,1.0),2*(t-0.5),if(lt(t,4.0),1,2*(5.0-t))))',"
            f"drawtext=text='{title_escaped}'"
            f":fontcolor=white:fontsize=32:x=(w-text_w)/2:y=h/2"
            f":alpha='if(lt(t,0.5),0,if(lt(t,1.0),2*(t-0.5),if(lt(t,4.0),1,2*(5.0-t))))'"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-t", "5", out,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _build_outro_slate(
    channel_name: str,
    social_handle: str,
    tmpdir: str,
    accent_color: str = "#C8A96E",
) -> str:
    out = os.path.join(tmpdir, "outro_slate.mp4")
    channel_escaped = channel_name.replace("'", "\\'")
    social_escaped = social_handle.replace("'", "\\'")
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi",
        "-i", "color=c=black:size=1920x1080:duration=8:rate=25",
        "-vf", (
            f"drawtext=text='Thanks for watching!'"
            f":fontcolor=white:fontsize=56:x=(w-text_w)/2:y=h/2-100"
            f":alpha='if(lt(t,0.5),0,if(lt(t,1.0),2*(t-0.5),if(lt(t,7.0),1,2*(8.0-t))))',"
            f"drawtext=text='{channel_escaped}'"
            f":fontcolor={accent_color}:fontsize=40:x=(w-text_w)/2:y=h/2"
            f":alpha='if(lt(t,0.5),0,if(lt(t,1.0),2*(t-0.5),if(lt(t,7.0),1,2*(8.0-t))))',"
            f"drawtext=text='{social_escaped}'"
            f":fontcolor=#AAAAAA:fontsize=28:x=(w-text_w)/2:y=h/2+80"
            f":alpha='if(lt(t,0.5),0,if(lt(t,1.0),2*(t-0.5),if(lt(t,7.0),1,2*(8.0-t))))'"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-t", "8", out,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _build_overlay_filter(overlay_type: str, overlay_text: str, accent_color: str) -> str:
    if overlay_type == "lower_third" and overlay_text:
        text_esc = overlay_text[:45].replace("'", "\\'").replace(":", "\\:")
        return (
            f"drawbox=y=ih-95:color=black@0.7:width=iw:height=95:t=fill,"
            f"drawtext=text='{text_esc}':fontcolor=white:fontsize=36"
            f":x=40:y=ih-75:shadowcolor=black:shadowx=2:shadowy=2"
        )
    elif overlay_type == "stat_counter" and overlay_text:
        text_esc = overlay_text[:45].replace("'", "\\'").replace(":", "\\:")
        return (
            f"drawtext=text='{text_esc}':fontcolor=white:fontsize=80"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
            f":shadowcolor=black@0.8:shadowx=4:shadowy=4"
        )
    elif overlay_type == "quote_card" and overlay_text:
        text_esc = overlay_text[:45].replace("'", "\\'").replace(":", "\\:")
        return (
            f"drawbox=x=(iw-800)/2:y=(ih-180)/2:width=800:height=180"
            f":color=black@0.65:t=fill,"
            f"drawtext=text='{text_esc}':fontcolor=white:fontsize=32"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
        )
    return ""


def _apply_transition(
    clip_a: str,
    clip_b: str,
    transition: str,
    duration: float,
    tmpdir: str,
    idx: int,
) -> str:
    out = os.path.join(tmpdir, f"transition_{idx:03d}.mp4")
    dur_a = _get_duration(clip_a)
    offset = max(0, dur_a - duration)

    xfade_map = {
        "crossfade": "dissolve",
        "dissolve": "dissolve",
        "zoom_punch": "smoothup",
        "whip": "slideleft",
        "cut": None,
    }
    xfade_name = xfade_map.get(transition)

    if xfade_name is None:
        list_file = os.path.join(tmpdir, f"concat_{idx}.txt")
        with open(list_file, "w") as f:
            f.write(f"file '{clip_a}'\n")
            f.write(f"file '{clip_b}'\n")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", out],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-i", clip_a, "-i", clip_b,
                "-filter_complex",
                f"[0][1]xfade=transition={xfade_name}:duration={duration}:offset={offset}[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                out,
            ],
            check=True, capture_output=True,
        )
    return out


def _submit_mediaconvert_job(
    input_s3_uri: str,
    output_s3_prefix: str,
    run_id: str,
) -> str:
    mc = boto3.client("mediaconvert", endpoint_url=_get_mediaconvert_endpoint())
    job_settings = {
        "Inputs": [
            {
                "FileInput": input_s3_uri,
                "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
                "VideoSelector": {},
            }
        ],
        "OutputGroups": [
            {
                "Name": "File Group",
                "OutputGroupSettings": {
                    "Type": "FILE_GROUP_SETTINGS",
                    "FileGroupSettings": {"Destination": output_s3_prefix},
                },
                "Outputs": [
                    {
                        "VideoDescription": {
                            "CodecSettings": {
                                "Codec": "H_264",
                                "H264Settings": {
                                    "Bitrate": 8000000,
                                    "CodecLevel": "AUTO",
                                    "CodecProfile": "HIGH",
                                    "RateControlMode": "CBR",
                                    "FramerateControl": "INITIALIZE_FROM_SOURCE",
                                },
                            },
                            "Width": 1920,
                            "Height": 1080,
                        },
                        "AudioDescriptions": [
                            {
                                "CodecSettings": {
                                    "Codec": "AAC",
                                    "AacSettings": {
                                        "Bitrate": 192000,
                                        "CodingMode": "CODING_MODE_2_0",
                                        "SampleRate": 48000,
                                    },
                                }
                            }
                        ],
                        "ContainerSettings": {
                            "Container": "MP4",
                            "Mp4Settings": {},
                        },
                    }
                ],
            }
        ],
    }

    role_arn = os.environ.get("MEDIACONVERT_ROLE_ARN", "")
    job = mc.create_job(Role=role_arn, Settings=job_settings)
    job_id = job["Job"]["Id"]

    deadline = time.time() + 1800
    while time.time() < deadline:
        time.sleep(30)
        status = mc.get_job(Id=job_id)["Job"]["Status"]
        if status == "COMPLETE":
            return f"{output_s3_prefix}final_video.mp4"
        if status in ("ERROR", "CANCELED"):
            raise RuntimeError(f"MediaConvert job {job_id} failed: {status}")

    raise TimeoutError(f"MediaConvert job {job_id} did not complete in 30 min")


def _get_mediaconvert_endpoint() -> str:
    mc = boto3.client("mediaconvert")
    endpoints = mc.describe_endpoints()
    return endpoints["Endpoints"][0]["Url"]


def _write_error(run_id: str, step: str, exc: Exception) -> None:
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_OUTPUTS_BUCKET,
            Key=f"{run_id}/errors/{step}.json",
            Body=json.dumps({"step": step, "error": str(exc)}).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception:
        pass


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event["run_id"]
    profile_name: str = event.get("profile", "documentary")
    sections: list[dict] = event.get("sections", [])
    mixed_audio_s3_key: str = event["mixed_audio_s3_key"]
    script_s3_key: str = event["script_s3_key"]
    dry_run: bool = event.get("dry_run", False)
    title_passthrough: str = event.get("title", "")

    try:
        s3 = boto3.client("s3")

        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        profile_obj = s3.get_object(Bucket=S3_CONFIG_BUCKET, Key=f"{profile_name}.json")
        profile: dict = json.loads(profile_obj["Body"].read())

        editing_cfg = profile.get("editing", {})
        j_cut_enabled = editing_cfg.get("j_cut_enabled", False)
        l_cut_enabled = editing_cfg.get("l_cut_enabled", False)
        j_cut_overlap = editing_cfg.get("j_cut_overlap_sec", 0.8)
        l_cut_overlap = editing_cfg.get("l_cut_overlap_sec", 1.0)
        default_transition = editing_cfg.get("default_transition", "dissolve")
        transition_dur = editing_cfg.get("transition_duration_sec", 1.0)
        beat_sync = editing_cfg.get("beat_sync_cuts", True)

        thumbnail_cfg = profile.get("thumbnail", {})
        accent_color = thumbnail_cfg.get("accent_color", "#C8A96E")
        channel_name = profile.get("name", "Nexus Channel").title()

        total_est = script.get("total_duration_estimate", 600)

        if dry_run:
            final_key = f"{run_id}/final_video_dry_run.mp4"
            return {
                "run_id": run_id,
                "profile": profile_name,
                "dry_run": True,
                "script_s3_key": script_s3_key,
                "title": title_passthrough or script.get("title", ""),
                "final_video_s3_key": final_key,
                "video_duration_sec": total_est,
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_local = os.path.join(tmpdir, "mixed_audio.wav")
            s3.download_file(S3_ASSETS_BUCKET, mixed_audio_s3_key, audio_local)

            beats = _detect_beats(audio_local) if beat_sync else []

            intro_path = _build_intro_slate(
                channel_name, script.get("title", ""), tmpdir, accent_color
            )

            clip_paths: list[str] = []
            for sec in sections:
                clip_key = sec.get("clip_s3_key", "")
                if not clip_key:
                    continue
                local_clip = os.path.join(tmpdir, os.path.basename(clip_key))
                try:
                    s3.download_file(S3_ASSETS_BUCKET, clip_key, local_clip)
                except Exception:
                    continue

                overlay_type = sec.get("overlay_type", "none")
                overlay_text = sec.get("overlay_text", "")
                if overlay_type != "none" and overlay_text:
                    overlay_filter = _build_overlay_filter(
                        overlay_type, overlay_text, accent_color
                    )
                    if overlay_filter:
                        overlaid = os.path.join(
                            tmpdir, f"overlaid_{len(clip_paths):03d}.mp4"
                        )
                        subprocess.run(
                            [FFMPEG_BIN, "-y", "-i", local_clip,
                             "-vf", overlay_filter,
                             "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                             overlaid],
                            check=True, capture_output=True,
                        )
                        clip_paths.append(overlaid)
                        continue

                clip_paths.append(local_clip)

            outro_path = _build_outro_slate(
                channel_name, f"@{channel_name.lower().replace(' ', '')}", tmpdir, accent_color
            )

            all_clips = [intro_path] + clip_paths + [outro_path]

            if len(all_clips) < 2:
                assembled = all_clips[0] if all_clips else intro_path
            else:
                current = all_clips[0]
                for i, next_clip in enumerate(all_clips[1:], 1):
                    transition_to_use = (
                        sections[i - 1].get("transition_in", default_transition)
                        if i - 1 < len(sections)
                        else default_transition
                    )
                    current = _apply_transition(
                        current, next_clip, transition_to_use,
                        transition_dur, tmpdir, i
                    )
                assembled = current

            final_local = os.path.join(tmpdir, "final_video.mp4")
            subprocess.run(
                [
                    FFMPEG_BIN, "-y",
                    "-i", assembled,
                    "-i", audio_local,
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    final_local,
                ],
                check=True, capture_output=True,
            )

            video_dur = _get_duration(final_local)
            final_s3_key = f"{run_id}/final_video.mp4"

            if video_dur > MEDIACONVERT_THRESHOLD_SECONDS:
                raw_s3_key = f"{run_id}/raw_assembled.mp4"
                s3.upload_file(final_local, S3_OUTPUTS_BUCKET, raw_s3_key)
                output_prefix = f"s3://{S3_OUTPUTS_BUCKET}/{run_id}/"
                final_s3_key = _submit_mediaconvert_job(
                    f"s3://{S3_OUTPUTS_BUCKET}/{raw_s3_key}", output_prefix, run_id
                )
            else:
                s3.upload_file(final_local, S3_OUTPUTS_BUCKET, final_s3_key)

        return {
            "run_id": run_id,
            "profile": profile_name,
            "dry_run": False,
            "script_s3_key": script_s3_key,
            "title": script.get("title", "") or title_passthrough,
            "final_video_s3_key": final_s3_key,
            "video_duration_sec": video_dur,
        }

    except Exception as exc:
        _write_error(run_id, "editor", exc)
        raise
