import json
import math
import os
import subprocess
import tempfile
import time
import urllib.request
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


def _find_bin(name: str) -> str:
    """Locate a binary (ffmpeg / ffprobe) across Lambda-layer and system paths."""
    for candidate in (f"/opt/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    import shutil
    path = shutil.which(name)
    if path:
        return path
    raise FileNotFoundError(f"{name} not found. Install it or set the {name.upper()}_BIN env var.")


FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or _find_bin("ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or _find_bin("ffprobe")

# ── Default font for drawtext ──
def _find_font(name: str) -> str:
    """Search common font directories for a font file."""
    candidates = [
        f"/usr/share/fonts/dejavu-sans-fonts/{name}",
        f"/usr/share/fonts/dejavu/{name}",
        f"/usr/share/fonts/truetype/dejavu/{name}",
        f"/usr/share/fonts/{name}",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""

DRAWTEXT_FONT = _find_font("DejaVuSans-Bold.ttf")
DRAWTEXT_FONT_LIGHT = _find_font("DejaVuSans.ttf")


def _escape_drawtext(text: str) -> str:
    """Escape text for use inside an ffmpeg drawtext filter value.

    ffmpeg drawtext requires its own escaping layer on top of shell escaping.
    The following characters must be escaped:  \\ : ' % ; [ ]
    We also remove newlines to prevent filter parse errors.
    """
    text = text.replace("\\", "\\\\\\\\")   # backslash
    text = text.replace(":", "\\:")          # colon
    text = text.replace("'", "'\\\\\\''")   # apostrophe — end quote, escaped quote, restart
    text = text.replace("%", "%%")           # percent (strftime expansion)
    text = text.replace(";", "\\;")          # semicolon (filter separator)
    text = text.replace("[", "\\[")          # bracket
    text = text.replace("]", "\\]")
    text = text.replace("\n", " ")
    text = text.replace("\r", "")
    return text


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
    title_escaped = _escape_drawtext(video_title)
    channel_escaped = _escape_drawtext(channel_name)
    # Font file argument — fallback to fontsize-only if font doesn't exist
    font_arg = f":fontfile={DRAWTEXT_FONT}" if os.path.isfile(DRAWTEXT_FONT) else ""
    font_arg_light = f":fontfile={DRAWTEXT_FONT_LIGHT}" if os.path.isfile(DRAWTEXT_FONT_LIGHT) else ""
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi",
        "-i", "color=c=black:size=1920x1080:duration=6:rate=30",
        "-f", "lavfi",
        "-i", f"color=c={accent_color}@0.15:size=1920x4:duration=6:rate=30",
        "-filter_complex", (
            # Gradient line overlay at bottom third
            "[1:v]scale=1920:360[grad];"
            "[0:v][grad]overlay=0:H-360:format=auto[base];"
            # Cinematic letterbox bars
            "[base]drawbox=y=0:w=iw:h=80:color=black@0.9:t=fill,"
            "drawbox=y=ih-80:w=iw:h=80:color=black@0.9:t=fill,"
            # Subtle vignette
            "vignette=angle=PI/3:mode=backward,"
            # Channel name — fade in from top
            f"drawtext=text='{channel_escaped}'"
            f"{font_arg}"
            f":fontcolor={accent_color}:fontsize=52:x=(w-text_w)/2"
            f":y='if(lt(t,0.8),h/2-120+40*(0.8-t),h/2-120)'"
            f":alpha='if(lt(t,0.3),0,if(lt(t,1.0),(t-0.3)/0.7,if(lt(t,5.0),1,(6.0-t))))',"
            # Decorative accent line under channel name
            f"drawbox=x=(1920-400)/2:y=462:w=400:h=2:color={accent_color}@0.8:t=fill,"
            # Video title — fade in slightly after channel name
            f"drawtext=text='{title_escaped}'"
            f"{font_arg_light}"
            f":fontcolor=white:fontsize=38:x=(w-text_w)/2"
            f":y='if(lt(t,1.2),h/2+20+30*(1.2-t),h/2+20)'"
            f":alpha='if(lt(t,0.8),0,if(lt(t,1.5),(t-0.8)/0.7,if(lt(t,5.0),1,(6.0-t))))'"
            # Global fade-in / fade-out
            ",fade=t=in:st=0:d=0.5,fade=t=out:st=5.5:d=0.5[out]"
        ),
        "-map", "[out]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-t", "6", out,
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
    channel_escaped = _escape_drawtext(channel_name)
    social_escaped = _escape_drawtext(social_handle)
    thanks_escaped = _escape_drawtext("Thanks for watching")
    subscribe_escaped = _escape_drawtext("SUBSCRIBE for more")
    font_arg = f":fontfile={DRAWTEXT_FONT}" if os.path.isfile(DRAWTEXT_FONT) else ""
    font_arg_light = f":fontfile={DRAWTEXT_FONT_LIGHT}" if os.path.isfile(DRAWTEXT_FONT_LIGHT) else ""
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi",
        "-i", "color=c=black:size=1920x1080:duration=10:rate=30",
        "-vf", (
            # Cinematic letterbox bars
            "drawbox=y=0:w=iw:h=80:color=black@0.9:t=fill,"
            "drawbox=y=ih-80:w=iw:h=80:color=black@0.9:t=fill,"
            # Vignette
            "vignette=angle=PI/3:mode=backward,"
            # "Thanks for watching" — primary CTA
            f"drawtext=text='{thanks_escaped}'"
            f"{font_arg}"
            f":fontcolor=white:fontsize=60:x=(w-text_w)/2"
            f":y='if(lt(t,0.5),h/2-140+40*(0.5-t),h/2-140)'"
            f":alpha='if(lt(t,0.3),0,if(lt(t,1.0),(t-0.3)/0.7,if(lt(t,8.5),1,(10.0-t)/1.5)))',"
            # Accent line
            f"drawbox=x=(1920-500)/2:y=416:w=500:h=3:color={accent_color}@0.8:t=fill,"
            # Channel name
            f"drawtext=text='{channel_escaped}'"
            f"{font_arg}"
            f":fontcolor={accent_color}:fontsize=44:x=(w-text_w)/2"
            f":y=h/2-40"
            f":alpha='if(lt(t,0.8),0,if(lt(t,1.5),(t-0.8)/0.7,if(lt(t,8.5),1,(10.0-t)/1.5)))',"
            # Social handle
            f"drawtext=text='{social_escaped}'"
            f"{font_arg_light}"
            f":fontcolor=#AAAAAA:fontsize=30:x=(w-text_w)/2"
            f":y=h/2+30"
            f":alpha='if(lt(t,1.2),0,if(lt(t,2.0),(t-1.2)/0.8,if(lt(t,8.5),1,(10.0-t)/1.5)))',"
            # Subscribe CTA — pulsing accent color
            f"drawtext=text='{subscribe_escaped}'"
            f"{font_arg}"
            f":fontcolor={accent_color}:fontsize=36:x=(w-text_w)/2"
            f":y=h/2+110"
            f":alpha='if(lt(t,2.0),0,if(lt(t,2.8),(t-2.0)/0.8,"
            f"if(lt(t,8.5),0.7+0.3*sin(t*3),max(0,(10.0-t)/1.5))))'"
            # Fade in/out
            ",fade=t=in:st=0:d=0.8,fade=t=out:st=9.0:d=1.0"
        ),
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-t", "10", out,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _build_overlay_filter(overlay_type: str, overlay_text: str, accent_color: str) -> str:
    if overlay_type == "lower_third" and overlay_text:
        text_esc = _escape_drawtext(overlay_text[:60])
        font_arg = f":fontfile={DRAWTEXT_FONT}" if os.path.isfile(DRAWTEXT_FONT) else ""
        return (
            f"drawbox=y=ih-110:color=black@0.75:width=iw:height=110:t=fill,"
            f"drawbox=y=ih-110:color={accent_color}@0.9:width=6:height=110:t=fill,"
            f"drawtext=text='{text_esc}'{font_arg}:fontcolor=white:fontsize=36"
            f":x=50:y=ih-82:shadowcolor=black@0.6:shadowx=2:shadowy=2"
        )
    elif overlay_type == "stat_counter" and overlay_text:
        text_esc = _escape_drawtext(overlay_text[:45])
        font_arg = f":fontfile={DRAWTEXT_FONT}" if os.path.isfile(DRAWTEXT_FONT) else ""
        return (
            f"drawbox=x=(iw-600)/2:y=(ih-120)/2:w=600:h=120:color=black@0.5:t=fill,"
            f"drawtext=text='{text_esc}'{font_arg}:fontcolor=white:fontsize=80"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
            f":shadowcolor=black@0.8:shadowx=4:shadowy=4"
        )
    elif overlay_type == "quote_card" and overlay_text:
        text_esc = _escape_drawtext(overlay_text[:80])
        font_arg_light = f":fontfile={DRAWTEXT_FONT_LIGHT}" if os.path.isfile(DRAWTEXT_FONT_LIGHT) else ""
        return (
            f"drawbox=x=(iw-900)/2:y=(ih-200)/2:width=900:height=200"
            f":color=black@0.7:t=fill,"
            f"drawbox=x=(iw-900)/2:y=(ih-200)/2:width=900:height=4"
            f":color={accent_color}@0.8:t=fill,"
            f"drawbox=x=(iw-900)/2:y=(ih+200)/2-4:width=900:height=4"
            f":color={accent_color}@0.8:t=fill,"
            f"drawtext=text='{text_esc}'{font_arg_light}:fontcolor=white:fontsize=32"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
        )
    return ""


def _loop_clip_to_duration(clip_path: str, target_duration: float, tmpdir: str, idx: int) -> str:
    """Loop or extend a clip to fill the target duration using reverse-loop for seamlessness."""
    clip_dur = _get_duration(clip_path)
    if clip_dur >= target_duration - 0.5:
        # Clip is long enough — just trim to target
        out = os.path.join(tmpdir, f"looped_{idx:03d}.mp4")
        subprocess.run(
            [FFMPEG_BIN, "-y", "-i", clip_path, "-t", str(target_duration),
             "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-an", out],
            check=True, capture_output=True,
        )
        return out

    # Need to loop — use concat with crossfade between iterations for smooth looping
    loops_needed = math.ceil(target_duration / max(clip_dur, 1.0))
    list_file = os.path.join(tmpdir, f"loop_list_{idx}.txt")
    with open(list_file, "w") as f:
        for _ in range(loops_needed + 1):
            f.write(f"file '{clip_path}'\n")
    looped_raw = os.path.join(tmpdir, f"looped_raw_{idx:03d}.mp4")
    subprocess.run(
        [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
         "-t", str(target_duration),
         "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-an",
         looped_raw],
        check=True, capture_output=True,
    )
    return looped_raw


def _apply_j_cut(
    video_a: str, video_b: str, audio_b: str | None,
    overlap_sec: float, tmpdir: str, idx: int,
) -> str:
    """J-cut: audio from next clip starts before its video appears."""
    if not audio_b or overlap_sec <= 0:
        return video_a
    out = os.path.join(tmpdir, f"jcut_{idx:03d}.mp4")
    dur_a = _get_duration(video_a)
    audio_start = max(0.0, dur_a - overlap_sec)
    try:
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-i", video_a, "-i", audio_b,
                "-filter_complex",
                f"[1:a]adelay={int(audio_start * 1000)}|{int(audio_start * 1000)}[delayed];"
                f"[0:a][delayed]amix=inputs=2:duration=longest[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                out,
            ],
            check=True, capture_output=True,
        )
        return out
    except Exception:
        return video_a


def _apply_l_cut(
    video_a: str, video_b: str,
    overlap_sec: float, tmpdir: str, idx: int,
) -> str:
    """L-cut: audio from previous clip continues over next clip's video."""
    if overlap_sec <= 0:
        return video_b
    out = os.path.join(tmpdir, f"lcut_{idx:03d}.mp4")
    try:
        subprocess.run(
            [
                FFMPEG_BIN, "-y",
                "-i", video_b, "-i", video_a,
                "-filter_complex",
                f"[1:a]atrim=end={overlap_sec},asetpts=PTS-STARTPTS[tail_audio];"
                f"[0:a][tail_audio]amix=inputs=2:duration=first[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-t", str(_get_duration(video_b)),
                out,
            ],
            check=True, capture_output=True,
        )
        return out
    except Exception:
        return video_b


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
    offset = max(0.0, dur_a - duration)

    xfade_map = {
        "crossfade": "dissolve",
        "dissolve": "dissolve",
        "zoom_punch": "smoothup",
        "whip": "slideleft",
        "wipeleft": "wipeleft",
        "wiperight": "wiperight",
        "fade_black": "fadeblack",
        "fade_white": "fadewhite",
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
                "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                "-pix_fmt", "yuv420p",
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
                                    "Bitrate": 15000000,
                                    "CodecLevel": "AUTO",
                                    "CodecProfile": "HIGH",
                                    "RateControlMode": "QVBR",
                                    "QvbrSettings": {"QvbrQualityLevel": 8},
                                    "FramerateControl": "INITIALIZE_FROM_SOURCE",
                                    "GopSize": 2.0,
                                    "GopSizeUnits": "SECONDS",
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
                                        "Bitrate": 256000,
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


def _notify_discord(step: str, color: int, run_id: str, fields: list[dict], dry_run: bool = False) -> None:
    """Send a step-level Discord notification. Silently swallows errors."""
    if dry_run:
        return
    try:
        webhook_url = get_secret("nexus/discord_webhook_url").get("url", "")
        if not webhook_url:
            return
        embed = {
            "embeds": [{
                "title": f"✂️ Nexus Cloud — {step}",
                "color": color,
                "fields": [{"name": "Run ID", "value": run_id, "inline": False}] + fields,
                "footer": {"text": "Nexus Cloud Pipeline"},
            }]
        }
        data = json.dumps(embed).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "NexusCloud/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
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
                # Gather all clip keys for this section (multi-clip support)
                clip_keys = sec.get("clip_s3_keys", [])
                single_key = sec.get("clip_s3_key", "")
                if not clip_keys and single_key:
                    clip_keys = [single_key]
                if not clip_keys:
                    continue

                section_duration = float(sec.get("duration_estimate_sec", 10))
                overlay_type = sec.get("overlay_type", "none")
                overlay_text = sec.get("overlay_text", "")

                section_clips: list[str] = []
                for ck_idx, clip_key in enumerate(clip_keys):
                    local_clip = os.path.join(tmpdir, f"sec{len(clip_paths):03d}_{ck_idx}.mp4")
                    try:
                        s3.download_file(S3_ASSETS_BUCKET, clip_key, local_clip)
                    except Exception:
                        continue
                    section_clips.append(local_clip)

                if not section_clips:
                    continue

                # Concatenate all section clips if multiple
                if len(section_clips) == 1:
                    section_video = section_clips[0]
                else:
                    concat_list = os.path.join(tmpdir, f"seccat_{len(clip_paths):03d}.txt")
                    with open(concat_list, "w") as f:
                        for sc in section_clips:
                            f.write(f"file '{sc}'\n")
                    section_video = os.path.join(tmpdir, f"seccat_{len(clip_paths):03d}.mp4")
                    subprocess.run(
                        [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
                         "-i", concat_list, "-c", "copy", section_video],
                        check=True, capture_output=True,
                    )

                # Loop/extend clip to fill narration duration for this section
                if section_duration > 1.0:
                    section_video = _loop_clip_to_duration(
                        section_video, section_duration, tmpdir, len(clip_paths)
                    )

                # Apply overlay if configured
                if overlay_type != "none" and overlay_text:
                    overlay_filter = _build_overlay_filter(
                        overlay_type, overlay_text, accent_color
                    )
                    if overlay_filter:
                        overlaid = os.path.join(
                            tmpdir, f"overlaid_{len(clip_paths):03d}.mp4"
                        )
                        subprocess.run(
                            [FFMPEG_BIN, "-y", "-i", section_video,
                             "-vf", overlay_filter,
                             "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                             "-pix_fmt", "yuv420p",
                             overlaid],
                            check=True, capture_output=True,
                        )
                        clip_paths.append(overlaid)
                        continue

                clip_paths.append(section_video)

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
                    "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "256k",
                    "-map_metadata", "-1",
                    "-movflags", "+faststart",
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

        _notify_discord("Video Assembled", 0xE74C3C, run_id, [
            {"name": "Title", "value": (script.get("title", "") or title_passthrough)[:100], "inline": False},
            {"name": "Duration", "value": f"{int(video_dur // 60)}m {int(video_dur % 60)}s", "inline": True},
            {"name": "Clips", "value": str(len(clip_paths)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], dry_run=dry_run)

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
