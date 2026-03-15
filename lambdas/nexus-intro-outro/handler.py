"""nexus-intro-outro — Generate channel intro/outro video clips.

ECS Fargate task entry point: handler(event, context)
Lambda entry point (channel-setup compatibility): lambda_handler(event, context)

Produces motion-graphics intro (3-5s) and outro (5-8s) via FFmpeg.
ECS mode saves to s3://nexus-outputs/{run_id}/editor/intro.mp4 and outro.mp4.
Lambda mode saves to s3://nexus-assets/channels/{channel_id}/intro.mp4 and outro.mp4.
"""

import json
import os
import subprocess
import tempfile

import boto3

from aws_xray_sdk.core import xray_recorder, patch_all

from nexus_pipeline_utils import notify_step_start, notify_step_complete, get_logger

patch_all()

log = get_logger("nexus-intro-outro")

_cache: dict = {}
s3 = boto3.client("s3")

OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "")
ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "")
CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "")

_FONT_MONTSERRAT = "/usr/share/fonts/truetype/montserrat-bold.ttf"
_FONT_LIBERATION = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


def _font() -> str:
    """Return the best available font path."""
    if os.path.exists(_FONT_MONTSERRAT):
        return _FONT_MONTSERRAT
    if os.path.exists(_FONT_LIBERATION):
        return _FONT_LIBERATION
    return "Sans"


def handler(event: dict, context) -> dict:
    """ECS Fargate entry point. Saves intro/outro to {run_id}/editor/ in OUTPUTS_BUCKET."""
    run_id = event["run_id"]
    profile = event["profile"]
    channel_id = event.get("channel_id", "default")
    dry_run = event.get("dry_run", False)

    notify_step_start(run_id, "IntroOutro")

    try:
        if dry_run:
            log.info("[%s] dry_run=True, skipping intro/outro generation", run_id)
            return {**event, "intro_s3_key": None, "outro_s3_key": None}

        brand = _load_brand(profile, channel_id)
        logo_path = _get_logo(channel_id)

        with tempfile.TemporaryDirectory() as tmp:
            with xray_recorder.in_subsegment("intro-render"):
                intro_path = _build_intro(tmp, brand, logo_path, run_id)
            with xray_recorder.in_subsegment("outro-render"):
                outro_path = _build_outro(tmp, brand, logo_path, run_id)

            intro_key = f"{run_id}/editor/intro.mp4"
            outro_key = f"{run_id}/editor/outro.mp4"

            with xray_recorder.in_subsegment("s3-upload"):
                s3.upload_file(intro_path, OUTPUTS_BUCKET, intro_key)
                s3.upload_file(outro_path, OUTPUTS_BUCKET, outro_key)

        notify_step_complete(run_id, "IntroOutro", {"intro": intro_key, "outro": outro_key})
        return {**event, "intro_s3_key": intro_key, "outro_s3_key": outro_key}

    except Exception as e:
        log.warning("[%s] intro/outro generation failed (non-fatal): %s", run_id, e)
        return {**event, "intro_s3_key": None, "outro_s3_key": None}


def lambda_handler(event: dict, context) -> dict:
    """Lambda entry point for channel-setup invocation.

    Accepts: channel_id, channel_name, niche, profile, brand.
    Saves intro/outro to channels/{channel_id}/ in ASSETS_BUCKET.
    """
    channel_id = event["channel_id"]
    channel_name = event.get("channel_name", channel_id)
    profile = event.get("profile", "documentary")
    brand_in = event.get("brand", {})

    brand = {
        "channel_name": channel_name,
        "profile": profile,
        "primary_color": brand_in.get("primary_color", "E8593C").lstrip("#"),
        "channel_cta": brand_in.get("tagline", ""),
    }

    log.info(
        "Generating channel intro/outro for '%s' (id=%s, profile=%s)",
        channel_name, channel_id, profile,
    )

    logo_path = _get_logo(channel_id)
    run_id = f"channel-{channel_id}"

    with tempfile.TemporaryDirectory() as tmp:
        intro_path = _build_intro(tmp, brand, logo_path, run_id)
        outro_path = _build_outro(tmp, brand, logo_path, run_id)

        intro_key = f"channels/{channel_id}/intro.mp4"
        outro_key = f"channels/{channel_id}/outro.mp4"

        s3.upload_file(intro_path, ASSETS_BUCKET, intro_key)
        s3.upload_file(outro_path, ASSETS_BUCKET, outro_key)

    log.info("[%s] intro/outro uploaded: intro=%s outro=%s", channel_id, intro_key, outro_key)
    return {
        "channel_id": channel_id,
        "intro_s3_key": intro_key,
        "outro_s3_key": outro_key,
    }


def _load_brand(profile: str, channel_id: str) -> dict:
    """Load brand kit from CONFIG_BUCKET. Returns defaults if not found."""
    try:
        obj = s3.get_object(Bucket=CONFIG_BUCKET, Key=f"channels/{channel_id}/brand.json")
        return json.loads(obj["Body"].read())
    except Exception:
        pass
    try:
        obj = s3.get_object(Bucket=CONFIG_BUCKET, Key=f"profiles/{profile}.json")
        p = json.loads(obj["Body"].read())
        return {
            "channel_name": p.get("channel_name", profile.title()),
            "profile": profile,
            "primary_color": p.get("brand", {}).get("primary_color", "E8593C").lstrip("#"),
            "channel_cta": p.get("channel_cta", ""),
        }
    except Exception:
        return {
            "channel_name": profile.title(),
            "profile": profile,
            "primary_color": "E8593C",
            "channel_cta": "",
        }


def _get_logo(channel_id: str) -> str | None:
    """Download logo PNG from ASSETS_BUCKET to /tmp. Returns local path or None."""
    try:
        local = f"/tmp/logo_{channel_id}.png"
        s3.download_file(ASSETS_BUCKET, f"channels/{channel_id}/logo.png", local)
        return local
    except Exception:
        return None


def _run(cmd: list, label: str) -> None:
    """Run a subprocess command. Raises RuntimeError on non-zero exit."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr
        # Preserve both start and end of stderr for diagnostics
        if len(stderr) > 1000:
            stderr = stderr[:400] + "\n...\n" + stderr[-400:]
        raise RuntimeError(f"FFmpeg {label} failed: {stderr}")


def _build_intro(tmp: str, brand: dict, logo_path: str | None, run_id: str) -> str:
    """Build intro.mp4 using FFmpeg motion graphics. Returns local path to intro.mp4."""
    channel_name = brand.get("channel_name", "Channel")
    primary_color = brand.get("primary_color", "E8593C").lstrip("#")
    profile = brand.get("profile", "documentary")

    duration = 4  # seconds (within 3-5s spec)
    fps = 30
    width, height = 1920, 1080
    font = _font()
    bar_color = primary_color if len(primary_color) == 6 else "E8593C"

    # ── Audio: whoosh sweep (logo entrance at 0.0s) ──────────────
    whoosh_wav = os.path.join(tmp, "whoosh.wav")
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=80:duration=0.8",
        "-af", (
            "afade=t=in:st=0:d=0.1,afade=t=out:st=0.6:d=0.2,"
            "aecho=0.8:0.88:60:0.4,equalizer=f=200:t=h:width=200:g=6"
        ),
        whoosh_wav,
    ], "whoosh")

    # ── Audio: sparkle hit (logo lands at 1.05s) ────────────────
    sparkle_wav = os.path.join(tmp, "sparkle.wav")
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=2800:duration=0.15",
        "-af", "afade=t=in:st=0:d=0.02,afade=t=out:st=0.08:d=0.07,aecho=0.6:0.5:20:0.3",
        sparkle_wav,
    ], "sparkle")

    # ── Audio: low cinematic boom (documentary/finance only) ─────
    boom_wav = os.path.join(tmp, "boom.wav")
    use_boom = profile != "entertainment"
    if use_boom:
        _run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=40:duration=1.2",
            "-af", (
                "afade=t=in:st=0:d=0.05,afade=t=out:st=0.8:d=0.4,"
                "equalizer=f=60:t=h:width=80:g=8,lowpass=f=120"
            ),
            boom_wav,
        ], "boom")

    # ── Audio: mix all elements ──────────────────────────────────
    mixed_wav = os.path.join(tmp, "intro_audio.wav")
    if use_boom:
        audio_filter = (
            "[0:a]adelay=0|0,volume=0.7[w];"
            "[1:a]adelay=1050|1050,volume=0.5[sp];"
            "[2:a]adelay=0|0,volume=0.4[b];"
            "[w][sp][b]amix=inputs=3:normalize=0,"
            "aloudnorm=I=-14:TP=-1.5:LRA=11,"
            "aformat=channel_layouts=stereo:sample_rates=48000[aout]"
        )
        _run([
            "ffmpeg", "-y",
            "-i", whoosh_wav, "-i", sparkle_wav, "-i", boom_wav,
            "-filter_complex", audio_filter,
            "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration), mixed_wav,
        ], "intro_audio_mix")
    else:
        audio_filter = (
            "[0:a]adelay=0|0,volume=0.7[w];"
            "[1:a]adelay=1050|1050,volume=0.5[sp];"
            "[w][sp]amix=inputs=2:normalize=0,"
            "aloudnorm=I=-14:TP=-1.5:LRA=11,"
            "aformat=channel_layouts=stereo:sample_rates=48000[aout]"
        )
        _run([
            "ffmpeg", "-y",
            "-i", whoosh_wav, "-i", sparkle_wav,
            "-filter_complex", audio_filter,
            "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration), mixed_wav,
        ], "intro_audio_mix")

    # ── Video: build with filter_complex ────────────────────────
    intro_mp4 = os.path.join(tmp, "intro.mp4")
    bg_color = "0x0a0a0a"

    # Particle field via geq + motion blur trails
    particles_vf = (
        "geq=lum='if(lt(random(1)*1000\\,1)\\,255\\,p(X\\,Y))':cb=128:cr=128,"
        "tmix=frames=8:weights='1 1 1 1 1 1 1 1'"
    )

    # Text safe for FFmpeg drawtext
    text_safe = channel_name.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    text_color_expr = "white@'if(lt(t\\,1.2)\\,0\\,min(1\\,(t-1.2)/0.4))'"

    # Logo scale animation (3-stage entrance)
    logo_scale = (
        "if(lt(t\\,0.8)\\,0.3+0.8*(t/0.8)\\,"
        "if(between(t\\,0.8\\,1.1)\\,1.1-0.1*((t-0.8)/0.3)\\,1.0))"
    )

    if logo_path:
        filter_complex = (
            f"[0:v]format=yuv420p[bg];"
            f"[bg]{particles_vf}[particles];"
            f"[1:v]scale=w='iw*({logo_scale})':h=-1,format=yuva420p[logo_sc];"
            f"[particles][logo_sc]overlay=x='(W-w)/2':y='H*0.42-h/2':format=auto[with_logo];"
            f"[with_logo]"
            f"drawtext=fontfile={font}:text='{text_safe}':fontsize=52:"
            f"fontcolor={text_color_expr}:x='(W-text_w)/2':y='H*0.62',"
            f"drawbox=x='(W-360)/2':y='H*0.62+60':"
            f"w='min(360\\,(360)*max(0\\,(t-1.4)/0.3))':h=3:"
            f"color=0x{bar_color}@1.0:t=fill[vout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={bg_color}:s={width}x{height}:r={fps}",
            "-loop", "1", "-i", logo_path,
            "-i", mixed_wav,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "2:a",
            "-c:v", "libx264", "-profile:v", "high",
            "-crf", "18", "-preset", "fast",
            "-r", str(fps), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            intro_mp4,
        ]
    else:
        filter_complex = (
            f"[0:v]format=yuv420p[bg];"
            f"[bg]{particles_vf}[particles];"
            f"[particles]"
            f"drawtext=fontfile={font}:text='{text_safe}':fontsize=52:"
            f"fontcolor={text_color_expr}:x='(W-text_w)/2':y='H*0.50',"
            f"drawbox=x='(W-360)/2':y='H*0.50+60':"
            f"w='min(360\\,(360)*max(0\\,(t-1.4)/0.3))':h=3:"
            f"color=0x{bar_color}@1.0:t=fill[vout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={bg_color}:s={width}x{height}:r={fps}",
            "-i", mixed_wav,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "1:a",
            "-c:v", "libx264", "-profile:v", "high",
            "-crf", "18", "-preset", "fast",
            "-r", str(fps), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            intro_mp4,
        ]

    _run(cmd, "intro_composite")
    log.info("[%s] intro built: %s", run_id, intro_mp4)
    return intro_mp4


def _build_outro(tmp: str, brand: dict, logo_path: str | None, run_id: str) -> str:
    """Build outro.mp4 using FFmpeg motion graphics. Returns local path to outro.mp4."""
    channel_name = brand.get("channel_name", "Channel")
    primary_color = brand.get("primary_color", "E8593C").lstrip("#")
    channel_cta = brand.get("channel_cta", "")

    duration = 7  # seconds (within 5-8s spec)
    fps = 30
    width, height = 1920, 1080
    font = _font()
    bar_color = primary_color if len(primary_color) == 6 else "E8593C"

    # ── Audio: ambient pad (3 sine waves, harmonically related) ─
    outro_pad_wav = os.path.join(tmp, "outro_pad.wav")
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-filter_complex",
        (
            "amix=inputs=3:weights='0.5 0.3 0.2':normalize=0,"
            "afade=t=in:st=0:d=1.5,"
            f"afade=t=out:st={duration - 2}:d=2,"
            "equalizer=f=300:t=h:width=400:g=-6,"
            "aecho=0.6:0.5:500:0.3,"
            "lowpass=f=800,"
            "volume=0.35[apad]"
        ),
        "-map", "[apad]", "-t", str(duration), outro_pad_wav,
    ], "outro_pad")

    # ── Audio: notification ping at 0.3s ─────────────────────────
    ping_wav = os.path.join(tmp, "ping.wav")
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=1200:duration=0.2",
        "-af", "afade=t=in:st=0:d=0.02,afade=t=out:st=0.15:d=0.05,volume=0.6",
        ping_wav,
    ], "ping")

    # ── Audio: mix pad + ping ─────────────────────────────────────
    mixed_wav = os.path.join(tmp, "outro_audio.wav")
    _run([
        "ffmpeg", "-y",
        "-i", outro_pad_wav, "-i", ping_wav,
        "-filter_complex",
        (
            "[1:a]adelay=300|300[ping_d];"
            "[0:a][ping_d]amix=inputs=2:normalize=0,"
            "aloudnorm=I=-14:TP=-1.5:LRA=11,"
            "aformat=channel_layouts=stereo:sample_rates=48000[aout]"
        ),
        "-map", "[aout]", "-c:a", "aac", "-b:a", "192k",
        "-t", str(duration), mixed_wav,
    ], "outro_audio_mix")

    # ── Video: build with filter_complex ────────────────────────
    outro_mp4 = os.path.join(tmp, "outro.mp4")
    bg_color = "0x0d0d0d"

    text_safe = channel_name.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    subscribe_fade = "if(lt(t\\,0.3)\\,0\\,min(1\\,(t-0.3)/0.5))"
    channel_fade = "if(lt(t\\,0.6)\\,0\\,min(1\\,(t-0.6)/0.4))"
    cta_fade = "if(lt(t\\,1.0)\\,0\\,min(1\\,(t-1.0)/0.3))"

    # Gradient sweep background
    gradient_filter = (
        f"geq=lum='lum(X\\,Y)+20*sin(2*PI*(X/{width}+Y/{height}-t*0.3))':cb=128:cr=128,"
        "vignette=PI/5"
    )

    # Build filter chain
    video_filters = [
        f"[0:v]format=yuv420p,{gradient_filter}[bg]",
        # Subscribe for more text
        (
            f"[bg]drawtext=fontfile={font}:text='Subscribe for more':fontsize=64:"
            f"fontcolor=white@'{subscribe_fade}':x='(W-text_w)/2':y='H*0.35'[t1]"
        ),
        # Channel name in brand color
        (
            f"[t1]drawtext=fontfile={font}:text='{text_safe}':fontsize=42:"
            f"fontcolor=0x{bar_color}@'{channel_fade}':x='(W-text_w)/2':y='H*0.46'[t2]"
        ),
        # Subscribe button background
        (
            f"[t2]drawbox=x='(W-280)/2':y='H*0.56':w=280:h=64:"
            f"color=cc0000@'{subscribe_fade}':t=fill[btn]"
        ),
        # Bell icon (small white box)
        (
            f"[btn]drawbox=x='(W-280)/2+18':y='H*0.56+16':w=24:h=24:"
            f"color=white@'{subscribe_fade}':t=fill[bell]"
        ),
        # SUBSCRIBE text inside button
        (
            f"[bell]drawtext=fontfile={font}:text='SUBSCRIBE':fontsize=26:"
            f"fontcolor=white@'{subscribe_fade}':x='(W-280)/2+60':y='H*0.56+20'[btn_done]"
        ),
    ]

    if logo_path:
        video_filters += [
            # Logo: fade in + oscillating rotation
            (
                "[1:v]scale=160:-1,format=yuva420p,"
                f"rotate=angle='3*PI/180*sin(2*PI*t/3)':c=none:ow=rotw(iw):oh=roth(ih),"
                f"colorchannelmixer=aa='{cta_fade}'[logo_out]"
            ),
            (
                "[btn_done][logo_out]overlay=x='(W-w)/2':y='H*0.76':format=auto[logo_layer]"
            ),
        ]
        last = "[logo_layer]"
    else:
        last = "[btn_done]"

    if channel_cta:
        cta_safe = channel_cta.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
        video_filters.append(
            f"{last}drawtext=fontfile={font}:text='{cta_safe}':fontsize=28:"
            f"fontcolor=white@'min(0.7\\,{cta_fade})':x='(W-text_w)/2':y='H*0.88'[vout]"
        )
    else:
        video_filters.append(f"{last}null[vout]")

    filter_complex = ";".join(video_filters)

    if logo_path:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={bg_color}:s={width}x{height}:r={fps}",
            "-loop", "1", "-i", logo_path,
            "-i", mixed_wav,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "2:a",
            "-c:v", "libx264", "-profile:v", "high",
            "-crf", "18", "-preset", "fast",
            "-r", str(fps), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            outro_mp4,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c={bg_color}:s={width}x{height}:r={fps}",
            "-i", mixed_wav,
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "1:a",
            "-c:v", "libx264", "-profile:v", "high",
            "-crf", "18", "-preset", "fast",
            "-r", str(fps), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k",
            "-t", str(duration),
            outro_mp4,
        ]

    _run(cmd, "outro_composite")
    log.info("[%s] outro built: %s", run_id, outro_mp4)
    return outro_mp4
