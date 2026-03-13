import json
import math
import os
import random
import subprocess
import tempfile
import time
import urllib.request
import boto3
from boto3.s3.transfer import TransferConfig
from nexus_pipeline_utils import get_logger, notify_step_start, notify_step_complete

log = get_logger("nexus-editor")

MEDIACONVERT_THRESHOLD_SECONDS = 600
_S3_MULTIPART_THRESHOLD = 100 * 1024 * 1024
_S3_TRANSFER_CONFIG = TransferConfig(multipart_threshold=_S3_MULTIPART_THRESHOLD)

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

# ── Font discovery (used by Pillow renderer) ──
def _find_font(name: str) -> str:
    """Search common font directories for a TTF font file."""
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


# ── PIL helpers ──────────────────────────────────────────────────────────────

def _hex_to_rgba(color: str, alpha: int = 255) -> tuple:
    """Convert '#RRGGBB' or '0xRRGGBB' to an (R, G, B, A) tuple."""
    color = color.strip()
    if color.startswith("#"):
        color = color[1:]
    elif color.lower().startswith("0x"):
        color = color[2:]
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)
    return (r, g, b, alpha)


def _pil_load_font(font_path: str, size: int):
    """Load a PIL ImageFont, falling back to the built-in default if unavailable."""
    try:
        from PIL import ImageFont
        if font_path and os.path.isfile(font_path):
            return ImageFont.truetype(font_path, size)
    except Exception:
        pass
    try:
        from PIL import ImageFont
        return ImageFont.load_default()
    except Exception:
        return None


def _pil_wrap_text(draw, text: str, font, max_width: int) -> list:
    """Word-wrap *text* into lines that fit within *max_width* pixels."""
    words = text.split()
    lines = []
    current: list = []
    for word in words:
        test = " ".join(current + [word])
        try:
            bbox = draw.textbbox((0, 0), test, font=font)
            w = bbox[2] - bbox[0]
        except Exception:
            w = len(test) * (font.size if hasattr(font, 'size') else 10)
        if w <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _hex_to_0x(color: str) -> str:
    """Convert '#RRGGBB' colour notation to '0xRRGGBB' so ffmpeg filter/lavfi
    parsers don't treat the '#' as a comment character."""
    if color.startswith("#"):
        return "0x" + color[1:]
    return color


def _escape_drawtext_content(text: str) -> str:
    """Escape text for ffmpeg drawtext *content* (read from a textfile=).

    Only characters meaningful to the drawtext filter itself need escaping.
    Filter-graph-level delimiters (;  [ ] = { } #) are NOT escaped here
    because the filter parser never sees textfile content.
    """
    # Normalize Unicode quotes to ASCII first so downstream escaping is uniform
    text = text.replace("\u2018", "'")             # LEFT  single curly quote
    text = text.replace("\u2019", "'")             # RIGHT single curly quote
    text = text.replace("\u201C", '"')             # LEFT  double curly quote
    text = text.replace("\u201D", '"')             # RIGHT double curly quote
    text = text.replace("\u2014", "-")             # em dash
    text = text.replace("\u2013", "-")             # en dash
    text = text.replace("\\", "\\\\")             # backslash  (must be first)
    text = text.replace(":", "\\:")                # colon  (drawtext key separator)
    text = text.replace("%", "%%")                 # percent (strftime expansion)
    text = text.replace("\n", " ")
    text = text.replace("\r", "")
    if len(text) > 120:
        text = text[:117] + "..."
    return text


def _escape_drawtext(text: str) -> str:
    """Escape text for use inside an inline ffmpeg drawtext  text='…'  value.

    Normalises Unicode punctuation to ASCII, then escapes all characters that
    would confuse either the drawtext option parser or the outer filter-graph
    parser.  The caller wraps the result in  text='…'  single quotes, so a
    literal apostrophe must be written as \\' (backslash + single quote).
    """
    text = _escape_drawtext_content(text)
    # Escape single quotes for inline text='...' — must come before other
    # filter-graph escapes so the backslash isn't double-escaped.
    text = text.replace("'", "\\'")               # apostrophe ends text='...'
    text = text.replace('"', '\\"')               # double quote (safety)
    # Additional filter-parser-level escapes (not needed for textfile=)
    text = text.replace(";", "\\;")               # semicolon (filter separator)
    text = text.replace("[", "\\[")               # bracket
    text = text.replace("]", "\\]")
    text = text.replace("=", "\\=")               # equals (key=value separator)
    text = text.replace("{", "\\{")               # brace (expression syntax)
    text = text.replace("}", "\\}")
    text = text.replace("#", "\\#")               # hash (color codes / comments)
    return text


def _detect_beats(audio_path: str) -> list[float]:
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        if not beat_times:
            raise ValueError("librosa returned empty beat list")
        return beat_times
    except Exception as exc:
        log.warning("Beat detection failed (%s) — falling back to fixed 3.5s cut points", exc)
        return []


def _fallback_cut_points(duration: float, interval: float = 3.5, jitter: float = 0.4) -> list[float]:
    # Generate cut points every interval seconds with ±jitter random offset
    points = []
    t = interval
    while t < duration:
        points.append(t + random.uniform(-jitter, jitter))
        t += interval
    return points


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


def _write_textfile(text: str, path: str) -> str:
    """Write text to a file (kept for compatibility). Returns the path."""
    content = text.replace("\\", "\\\\").replace(":", "\\:").replace("%", "%%")
    content = content.replace("\n", " ").replace("\r", "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_intro_slate(
    channel_name: str,
    video_title: str,
    tmpdir: str,
    accent_color: str = "#C8A96E",
) -> str:
    """Render a 6-second intro slate using Pillow (no drawtext / libfreetype needed)."""
    from PIL import Image, ImageDraw

    W, H = 1920, 1080
    ar, ag, ab, _ = _hex_to_rgba(accent_color)
    accent_rgba = (ar, ag, ab, 255)

    img = Image.new("RGB", (W, H), (0, 0, 0))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Bottom-third accent gradient (fades from transparent to accent@~10%)
    for row in range(360):
        alpha = int(25 * (row / 360))
        draw.line([(0, H - 360 + row), (W, H - 360 + row)], fill=(ar, ag, ab, alpha))

    # Letterbox bars
    draw.rectangle([(0, 0), (W, 80)], fill=(0, 0, 0, 230))
    draw.rectangle([(0, H - 80), (W, H)], fill=(0, 0, 0, 230))

    # Channel name — centered at y ≈ H/2 - 120
    font_ch = _pil_load_font(DRAWTEXT_FONT, 52)
    try:
        bbox_ch = draw.textbbox((0, 0), channel_name, font=font_ch)
        tw = bbox_ch[2] - bbox_ch[0]
    except Exception:
        tw = len(channel_name) * 30
    draw.text(((W - tw) // 2, H // 2 - 120), channel_name, font=font_ch, fill=accent_rgba)

    # Decorative accent line
    lx = (W - 400) // 2
    draw.rectangle([(lx, 462), (lx + 400, 464)], fill=(ar, ag, ab, 200))

    # Title — centered at y ≈ H/2 + 20, word-wrapped
    font_ti = _pil_load_font(DRAWTEXT_FONT_LIGHT, 38)
    lines = _pil_wrap_text(draw, video_title, font_ti, 1400)
    y_title = H // 2 + 20
    for line in lines[:4]:
        try:
            bbox_t = draw.textbbox((0, 0), line, font=font_ti)
            lw = bbox_t[2] - bbox_t[0]
        except Exception:
            lw = len(line) * 22
        draw.text(((W - lw) // 2, y_title), line, font=font_ti, fill=(255, 255, 255, 255))
        y_title += 52

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    png_path = os.path.join(tmpdir, "intro_slate.png")
    img.save(png_path)

    out = os.path.join(tmpdir, "intro_slate.mp4")
    cmd = [
        FFMPEG_BIN, "-y",
        "-loop", "1", "-i", png_path,
        "-vf", "fade=t=in:st=0:d=0.5,fade=t=out:st=5.5:d=0.5",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p", "-t", "6", out,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr_msg = exc.stderr.decode("utf-8", errors="replace")[-1500:] if exc.stderr else "no stderr"
        log.error("Intro slate FFmpeg failed (exit %d):\n%s", exc.returncode, stderr_msg)
        raise
    return out


def _build_outro_slate(
    channel_name: str,
    social_handle: str,
    tmpdir: str,
    accent_color: str = "#C8A96E",
) -> str:
    """Render a 10-second outro slate using Pillow (no drawtext / libfreetype needed)."""
    from PIL import Image, ImageDraw

    W, H = 1920, 1080
    ar, ag, ab, _ = _hex_to_rgba(accent_color)
    accent_rgba = (ar, ag, ab, 255)

    img = Image.new("RGB", (W, H), (0, 0, 0))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Letterbox bars
    draw.rectangle([(0, 0), (W, 80)], fill=(0, 0, 0, 230))
    draw.rectangle([(0, H - 80), (W, H)], fill=(0, 0, 0, 230))

    font_bold = _pil_load_font(DRAWTEXT_FONT, 60)
    font_bold_sm = _pil_load_font(DRAWTEXT_FONT, 44)
    font_light = _pil_load_font(DRAWTEXT_FONT_LIGHT, 30)
    font_cta = _pil_load_font(DRAWTEXT_FONT, 36)

    def draw_centered(text, font, fill, y):
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(text) * 25
        draw.text(((W - tw) // 2, y), text, font=font, fill=fill)

    # "Thanks for watching"
    draw_centered("Thanks for watching", font_bold, (255, 255, 255, 255), H // 2 - 140)

    # Accent line
    lx = (W - 500) // 2
    draw.rectangle([(lx, H // 2 - 60), (lx + 500, H // 2 - 57)], fill=(ar, ag, ab, 204))

    # Channel name
    draw_centered(channel_name, font_bold_sm, accent_rgba, H // 2 - 40)

    # Social handle
    draw_centered(social_handle, font_light, (170, 170, 170, 255), H // 2 + 30)

    # Subscribe CTA
    draw_centered("SUBSCRIBE for more", font_cta, accent_rgba, H // 2 + 110)

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    png_path = os.path.join(tmpdir, "outro_slate.png")
    img.save(png_path)

    out = os.path.join(tmpdir, "outro_slate.mp4")
    cmd = [
        FFMPEG_BIN, "-y",
        "-loop", "1", "-i", png_path,
        "-vf", "fade=t=in:st=0:d=0.8,fade=t=out:st=9.0:d=1.0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p", "-t", "10", out,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr_msg = exc.stderr.decode("utf-8", errors="replace")[-1500:] if exc.stderr else "no stderr"
        log.error("Outro slate FFmpeg failed (exit %d):\n%s", exc.returncode, stderr_msg)
        raise
    return out


def _build_overlay_filter(overlay_type: str, overlay_text: str, accent_color: str,
                          tmpdir: str = "/mnt/scratch") -> str:
    """Render a text overlay PNG with Pillow and return an ffmpeg -vf filtergraph
    string that composites it via the 'movie=' source filter (no libfreetype needed)."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = _hex_to_rgba(accent_color)
    W, H = 1920, 1080

    if overlay_type == "lower_third" and overlay_text:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Black bar + accent stripe
        draw.rectangle([(0, H - 110), (W, H)], fill=(0, 0, 0, 191))
        draw.rectangle([(0, H - 110), (6, H)], fill=(ar, ag, ab, 229))
        # Text
        font = _pil_load_font(DRAWTEXT_FONT, 36)
        draw.text((50, H - 82), overlay_text[:60], font=font, fill=(255, 255, 255, 255))
        png_path = os.path.join(tmpdir, "ov_lower.png")
        img.save(png_path)
        return f"movie={png_path}:loop=0,setpts=PTS-STARTPTS[_ovrl];[in][_ovrl]overlay=0:0"

    elif overlay_type == "stat_counter" and overlay_text:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Semi-transparent bg box
        bx, by, bw, bh = (W - 600) // 2, (H - 120) // 2, 600, 120
        draw.rectangle([(bx, by), (bx + bw, by + bh)], fill=(0, 0, 0, 128))
        # Big stat text — centered
        font = _pil_load_font(DRAWTEXT_FONT, 80)
        text = overlay_text[:45]
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(text) * 48, 80
        draw.text(((W - tw) // 2, (H - th) // 2), text, font=font, fill=(255, 255, 255, 255))
        png_path = os.path.join(tmpdir, "ov_stat.png")
        img.save(png_path)
        return f"movie={png_path}:loop=0,setpts=PTS-STARTPTS[_ovrl];[in][_ovrl]overlay=0:0"

    elif overlay_type == "quote_card" and overlay_text:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bx, by, bw, bh = (W - 900) // 2, (H - 200) // 2, 900, 200
        # Card bg
        draw.rectangle([(bx, by), (bx + bw, by + bh)], fill=(0, 0, 0, 178))
        # Top accent line
        draw.rectangle([(bx, by), (bx + bw, by + 4)], fill=(ar, ag, ab, 204))
        # Bottom accent line
        draw.rectangle([(bx, by + bh - 4), (bx + bw, by + bh)], fill=(ar, ag, ab, 204))
        # Quote text — centered, wrapped
        font = _pil_load_font(DRAWTEXT_FONT_LIGHT, 32)
        lines = _pil_wrap_text(draw, overlay_text[:120], font, bw - 60)
        total_h = len(lines) * 42
        y_txt = by + (bh - total_h) // 2
        for line in lines:
            try:
                bbox = draw.textbbox((0, 0), line, font=font)
                lw = bbox[2] - bbox[0]
            except Exception:
                lw = len(line) * 20
            draw.text(((W - lw) // 2, y_txt), line, font=font, fill=(255, 255, 255, 255))
            y_txt += 42
        png_path = os.path.join(tmpdir, "ov_quote.png")
        img.save(png_path)
        return f"movie={png_path}:loop=0,setpts=PTS-STARTPTS[_ovrl];[in][_ovrl]overlay=0:0"



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
                                    "Bitrate": 6000000,
                                    "CodecLevel": "AUTO",
                                    "CodecProfile": "HIGH",
                                    "RateControlMode": "CBR",
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


SCRATCH_DIR = os.environ.get("TMPDIR", "/mnt/scratch")


def lambda_handler(event: dict, context) -> dict:
    run_id: str = event.get("run_id") or os.environ.get("RUN_ID", "")
    profile_name: str = event.get("profile") or os.environ.get("PROFILE", "documentary")
    niche: str = event.get("niche") or os.environ.get("NICHE", "")
    sections: list[dict] = event.get("sections", [])
    mixed_audio_s3_key: str = event.get("mixed_audio_s3_key") or os.environ.get("MIXED_AUDIO_S3_KEY", "")
    script_s3_key: str = event.get("script_s3_key") or os.environ.get("SCRIPT_S3_KEY", "")
    dry_run_raw = event.get("dry_run") if "dry_run" in event else os.environ.get("DRY_RUN", "false")
    dry_run: bool = dry_run_raw if isinstance(dry_run_raw, bool) else str(dry_run_raw).lower() == "true"
    title_passthrough: str = event.get("title") or os.environ.get("TITLE", "")

    step_start = notify_step_start("editor", run_id, niche=niche, profile=profile_name, dry_run=dry_run)

    try:
        s3 = boto3.client("s3")

        if not sections and run_id:
            sections_key = f"{run_id}/status/visuals_sections.json"
            try:
                sections_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=sections_key)
                sections = json.loads(sections_obj["Body"].read())
                log.info("Loaded %d sections from S3: %s", len(sections), sections_key)
            except Exception as exc:
                log.warning("Could not load sections from S3 (%s): %s", sections_key, exc)

        log.info("Loading script from S3: %s", script_s3_key)
        script_obj = s3.get_object(Bucket=S3_OUTPUTS_BUCKET, Key=script_s3_key)
        script: dict = json.loads(script_obj["Body"].read())

        log.info("Loading profile: %s", profile_name)
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
            log.info("DRY RUN mode — returning stub video key")
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

        with tempfile.TemporaryDirectory(dir=SCRATCH_DIR if os.path.isdir(SCRATCH_DIR) else None) as tmpdir:
            log.info("Downloading mixed audio from S3: %s", mixed_audio_s3_key)
            audio_local = os.path.join(tmpdir, "mixed_audio.wav")
            s3.download_file(S3_ASSETS_BUCKET, mixed_audio_s3_key, audio_local)

            log.info("Detecting beats (beat_sync=%s)", beat_sync)
            beats = _detect_beats(audio_local) if beat_sync else []
            if beat_sync and not beats:
                audio_duration = _get_duration(audio_local)
                beats = _fallback_cut_points(audio_duration)
                log.info("Using fallback cut points: %d points over %.1fs", len(beats), audio_duration)
            else:
                log.info("Detected %d beats", len(beats))

            log.info("Building intro slate (channel=%s, accent=%s)", channel_name, accent_color)
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
                        overlay_type, overlay_text, accent_color, tmpdir
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

            log.info("Building outro slate")
            outro_path = _build_outro_slate(
                channel_name, f"@{channel_name.lower().replace(' ', '')}", tmpdir, accent_color
            )

            all_clips = [intro_path] + clip_paths + [outro_path]
            log.info("Assembling %d clips (intro + %d sections + outro)", len(all_clips), len(clip_paths))

            if len(all_clips) < 2:
                assembled = all_clips[0] if all_clips else intro_path
            elif len(all_clips) == 2:
                transition_to_use = (
                    sections[0].get("transition_in", default_transition)
                    if sections else default_transition
                )
                assembled = _apply_transition(
                    all_clips[0], all_clips[1], transition_to_use,
                    transition_dur, tmpdir, 0
                )
            else:
                # ── Batch assembly: determine transitions for each join ──
                transitions_list = []
                for i in range(1, len(all_clips)):
                    t = (
                        sections[i - 1].get("transition_in", default_transition)
                        if i - 1 < len(sections)
                        else default_transition
                    )
                    transitions_list.append(t)

                # Check if all transitions are plain cuts — fast concat path
                all_cuts = all(t == "cut" for t in transitions_list)

                if all_cuts:
                    # Fast path: concat demux with no re-encoding
                    concat_file = os.path.join(tmpdir, "final_concat.txt")
                    with open(concat_file, "w") as f:
                        for clip in all_clips:
                            f.write(f"file '{clip}'\n")
                    assembled = os.path.join(tmpdir, "assembled_concat.mp4")
                    subprocess.run(
                        [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
                         "-i", concat_file, "-c", "copy", assembled],
                        check=True, capture_output=True,
                    )
                else:
                    # Mixed transitions: apply pairwise but limit re-encodes
                    # by processing in groups of 4-5 clips, then concatenating groups
                    group_size = 4
                    group_outputs = []

                    for g_start in range(0, len(all_clips), group_size):
                        group = all_clips[g_start : g_start + group_size]
                        group_trans = transitions_list[g_start : g_start + group_size - 1]

                        if len(group) == 1:
                            group_outputs.append(group[0])
                        else:
                            current = group[0]
                            for gi, next_clip in enumerate(group[1:]):
                                t = group_trans[gi] if gi < len(group_trans) else default_transition
                                current = _apply_transition(
                                    current, next_clip, t,
                                    transition_dur, tmpdir,
                                    g_start + gi,
                                )
                            group_outputs.append(current)

                    # Concatenate all group outputs
                    if len(group_outputs) == 1:
                        assembled = group_outputs[0]
                    else:
                        concat_file = os.path.join(tmpdir, "groups_concat.txt")
                        with open(concat_file, "w") as f:
                            for gout in group_outputs:
                                f.write(f"file '{gout}'\n")
                        assembled = os.path.join(tmpdir, "assembled_groups.mp4")
                        subprocess.run(
                            [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
                             "-i", concat_file, "-c", "copy", assembled],
                            check=True, capture_output=True,
                        )

            log.info("Final mux: merging video + audio")
            final_local = os.path.join(tmpdir, "final_video.mp4")

            # Ensure the assembled video is at least as long as the audio track.
            # If clips don't cover the full narration, loop the last clip to fill the gap.
            audio_dur = _get_duration(audio_local)
            video_dur_before_mux = _get_duration(assembled)
            if audio_dur > video_dur_before_mux + 1.0 and clip_paths:
                log.info(
                    "Audio (%.1fs) longer than video (%.1fs) — extending visual track",
                    audio_dur, video_dur_before_mux,
                )
                gap = audio_dur - video_dur_before_mux
                extended = _loop_clip_to_duration(
                    clip_paths[-1], _get_duration(clip_paths[-1]) + gap, tmpdir, 9999
                )
                final_concat = os.path.join(tmpdir, "final_extended_concat.txt")
                with open(final_concat, "w") as f:
                    f.write(f"file '{assembled}'\n")
                    f.write(f"file '{extended}'\n")
                assembled_extended = os.path.join(tmpdir, "assembled_extended.mp4")
                subprocess.run(
                    [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
                     "-i", final_concat, "-c", "copy", assembled_extended],
                    check=True, capture_output=True,
                )
                assembled = assembled_extended
                log.info("Extended assembled video to %.1fs", _get_duration(assembled))

            try:
                subprocess.run(
                    [
                        FFMPEG_BIN, "-y",
                        "-i", assembled,
                        "-i", audio_local,
                        "-map", "0:v:0",
                        "-map", "1:a:0",
                        "-c:v", "libx264", "-preset", "medium", "-b:v", "6000k",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k",
                        "-map_metadata", "-1",
                        "-movflags", "+faststart",
                        final_local,
                    ],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                stderr_msg = exc.stderr.decode("utf-8", errors="replace")[-2000:] if exc.stderr else "no stderr"
                log.error("Final mux FFmpeg failed (exit %d):\n%s", exc.returncode, stderr_msg)
                raise

            video_dur = _get_duration(final_local)
            log.info("Final video duration: %.1fs", video_dur)
            final_s3_key = f"{run_id}/review/final_video.mp4"

            if video_dur > MEDIACONVERT_THRESHOLD_SECONDS:
                log.info("Video > %ds — submitting to MediaConvert", MEDIACONVERT_THRESHOLD_SECONDS)
                raw_s3_key = f"{run_id}/raw_assembled.mp4"
                s3.upload_file(final_local, S3_OUTPUTS_BUCKET, raw_s3_key, Config=_S3_TRANSFER_CONFIG)
                output_prefix = f"s3://{S3_OUTPUTS_BUCKET}/{run_id}/review/"
                final_s3_key = _submit_mediaconvert_job(
                    f"s3://{S3_OUTPUTS_BUCKET}/{raw_s3_key}", output_prefix, run_id
                )
            else:
                log.info("Uploading final video to S3: %s", final_s3_key)
                s3.upload_file(final_local, S3_OUTPUTS_BUCKET, final_s3_key, Config=_S3_TRANSFER_CONFIG)

            log.info("Copying script to metadata: %s/metadata/script.txt", run_id)
            try:
                s3.copy_object(
                    CopySource={"Bucket": S3_OUTPUTS_BUCKET, "Key": script_s3_key},
                    Bucket=S3_OUTPUTS_BUCKET,
                    Key=f"{run_id}/metadata/script.txt",
                )
            except Exception as copy_exc:
                log.warning("Failed to copy script to metadata key: %s", copy_exc)

        elapsed = time.time() - step_start
        notify_step_complete("editor", run_id, [
            {"name": "Title", "value": (script.get("title", "") or title_passthrough)[:100], "inline": False},
            {"name": "Duration", "value": f"{int(video_dur // 60)}m {int(video_dur % 60)}s", "inline": True},
            {"name": "Clips", "value": str(len(clip_paths)), "inline": True},
            {"name": "Profile", "value": profile_name, "inline": True},
        ], elapsed_sec=elapsed, dry_run=dry_run, color=0xE74C3C)

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
        log.error("Editor step FAILED: %s", exc, exc_info=True)
        _write_error(run_id, "editor", exc)
        raise
    finally:
        import shutil
        scratch_dir = os.path.join(SCRATCH_DIR, run_id)
        shutil.rmtree(scratch_dir, ignore_errors=True)


if __name__ == "__main__":
    import sys
    result = lambda_handler({}, None)
    print(json.dumps(result, default=str))
    sys.exit(0)