"""Shared motion graphics and camera animation library.

Used by both nexus-editor (landscape 1920×1080) and nexus-shorts (vertical 1080×1920).
All overlays are rendered as Pillow PNG frame sequences — no libfreetype needed.
FFmpeg composites them via the overlay filter.
"""

from __future__ import annotations

import math
import os
from typing import Sequence

# ── Easing functions ─────────────────────────────────────────────────────────

def ease_out_expo(t: float) -> float:
    """Snappy deceleration — titles, reveals."""
    return 1.0 if t >= 1.0 else 1.0 - pow(2.0, -10.0 * t)


def ease_in_out_cubic(t: float) -> float:
    """Smooth in and out — holds, fades."""
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - pow(-2.0 * t + 2.0, 3) / 2.0


def ease_out_back(t: float, overshoot: float = 1.70158) -> float:
    """Slight overshoot — logos, pop-ins."""
    t = min(max(t, 0.0), 1.0)
    return 1.0 + (overshoot + 1.0) * pow(t - 1.0, 3) + overshoot * pow(t - 1.0, 2)


# ── Color helpers ────────────────────────────────────────────────────────────

def hex_to_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = color.strip().lstrip("#").lstrip("0x")
    if len(color) < 6:
        color = color.ljust(6, "0")
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    return (r, g, b, alpha)


# ── Font helpers ─────────────────────────────────────────────────────────────

def find_font(name: str) -> str:
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


FONT_BOLD = find_font("DejaVuSans-Bold.ttf")
FONT_LIGHT = find_font("DejaVuSans.ttf")


def load_font(font_path: str, size: int):
    """Load a PIL ImageFont, fallback to default."""
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


def wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        test = " ".join(current + [word])
        try:
            bbox = draw.textbbox((0, 0), test, font=font)
            w = bbox[2] - bbox[0]
        except Exception:
            w = len(test) * (font.size if hasattr(font, "size") else 10)
        if w <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


# ── Camera motion filters (FFmpeg) ──────────────────────────────────────────

def build_camera_motion_filter(
    style: str,
    clip_dur: float,
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Build video-safe camera motion filters.

    Uses scale + animated crop instead of zoompan (zoompan fails on video input).
    Supports both landscape (1920×1080) and vertical (1080×1920) outputs.
    """
    clip_dur = max(0.5, clip_dur)
    is_vertical = height > width

    if is_vertical:
        return _vertical_camera_motion(style, clip_dur, width, height)
    return _landscape_camera_motion(style, clip_dur, width, height)


def _landscape_camera_motion(style: str, dur: float, w: int, h: int) -> str:
    if style == "ken_burns_in":
        return (
            "scale=2048:1152,"
            f"crop=w='2048-128*t/{dur}':h='1152-72*t/{dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            f"scale={w}:{h}"
        )
    elif style == "ken_burns_out":
        return (
            "scale=2048:1152,"
            f"crop=w='1920+128*t/{dur}':h='1080+72*t/{dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            f"scale={w}:{h}"
        )
    elif style == "pan_left":
        return (
            f"scale=2160:{h}:force_original_aspect_ratio=increase,"
            f"scale='max(2160,iw)':'max({h},ih)',"
            f"crop={w}:{h}:x='min(iw-{w},max(0,(iw-{w})*t/{dur}))':y='(ih-{h})/2'"
        )
    elif style == "pan_right":
        return (
            f"scale=2160:{h}:force_original_aspect_ratio=increase,"
            f"scale='max(2160,iw)':'max({h},ih)',"
            f"crop={w}:{h}:x='max(0,(iw-{w})*(1-t/{dur}))':y='(ih-{h})/2'"
        )
    elif style == "slow_drift":
        return (
            "scale=2160:1216:force_original_aspect_ratio=increase,"
            "scale='max(2160,iw)':'max(1216,ih)',"
            f"crop={w}:{h}:x='min(iw-{w},max(0,(iw-{w})*t/{dur}*0.6))'"
            f":y='min(ih-{h},max(0,(ih-{h})*t/{dur}*0.4))'"
        )
    elif style == "dolly_in":
        return (
            "scale=2304:1296,"
            f"crop=w='2304-(384*t/{dur})':h='1296-(216*t/{dur})'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            f"scale={w}:{h}"
        )
    elif style == "parallax":
        return (
            "scale=2304:1296,"
            f"crop={w}:{h}"
            f":x='(iw-{w})/2+sin(t/{dur}*3.14159)*120'"
            f":y='(ih-{h})/2+cos(t/{dur}*3.14159)*60',"
            f"scale={w}:{h}"
        )
    elif style == "orbit":
        return (
            "scale=2400:1350,"
            f"crop={w}:{h}"
            f":x='(iw-{w})/2+sin(t/{dur}*6.28318)*150'"
            f":y='(ih-{h})/2+cos(t/{dur}*6.28318)*80',"
            f"scale={w}:{h}"
        )
    else:
        # static — just ensure correct dimensions
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"


def _vertical_camera_motion(style: str, dur: float, w: int, h: int) -> str:
    """Camera motions adapted for 1080×1920 vertical output."""
    if style == "dolly_in":
        return (
            f"scale={int(w * 1.3)}:{int(h * 1.3)},"
            f"crop=w='{int(w * 1.3)}-{int(w * 0.3)}*t/{dur}'"
            f":h='{int(h * 1.3)}-{int(h * 0.3)}*t/{dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            f"scale={w}:{h}"
        )
    elif style == "pan_sweep":
        return (
            f"scale={int(w * 1.5)}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}:x='min(iw-{w},max(0,(iw-{w})*t/{dur}))':y='(ih-{h})/2'"
        )
    elif style == "drift":
        return (
            f"scale={int(w * 1.2)}:{int(h * 1.2)},"
            f"crop={w}:{h}"
            f":x='(iw-{w})/2+sin(t/{dur}*3.14159)*{int(w * 0.05)}'"
            f":y='(ih-{h})/2+{int(h * 0.05)}*t/{dur}'"
        )
    elif style == "parallax":
        return (
            f"scale={int(w * 1.25)}:{int(h * 1.25)},"
            f"crop={w}:{h}"
            f":x='(iw-{w})/2+sin(t/{dur}*3.14159)*{int(w * 0.08)}'"
            f":y='(ih-{h})/2+cos(t/{dur}*3.14159)*{int(h * 0.03)}'"
        )
    elif style == "ken_burns_in":
        sw, sh = int(w * 1.15), int(h * 1.15)
        return (
            f"scale={sw}:{sh},"
            f"crop=w='{sw}-{sw - w}*t/{dur}':h='{sh}-{sh - h}*t/{dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            f"scale={w}:{h}"
        )
    elif style == "ken_burns_out":
        return (
            f"scale={int(w * 1.15)}:{int(h * 1.15)},"
            f"crop=w='{w}+{int(w * 0.15)}*t/{dur}':h='{h}+{int(h * 0.15)}*t/{dur}'"
            ":x='(iw-ow)/2':y='(ih-oh)/2',"
            f"scale={w}:{h}"
        )
    else:
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"


# ── Overlay frame-sequence renderers ─────────────────────────────────────────
# Each renderer returns a list of PNG file paths (one per frame at given fps).

def render_kinetic_title(
    title: str,
    subtitle: str,
    accent_color: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    out_dir: str,
) -> list[str]:
    """Animated title with slide-in, underline wipe, slide-out."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(duration * fps)
    paths: list[str] = []
    font_title = load_font(FONT_BOLD, max(36, height // 30))
    font_sub = load_font(FONT_LIGHT, max(24, height // 45))

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Card background
        card_h = height // 4
        card_y = height - card_h - height // 10
        card_alpha = int(180 * min(1.0, ease_out_expo(t / 0.15) if t < 0.15 else (1.0 if t < 0.75 else max(0, 1.0 - (t - 0.75) / 0.25))))
        draw.rectangle([(0, card_y), (width, card_y + card_h)], fill=(0, 0, 0, card_alpha))

        if t < 0.95:
            # Title slide in from left
            title_progress = ease_out_back(min(1.0, t / 0.15)) if t < 0.15 else 1.0
            slide_out = max(0.0, (t - 0.95) / 0.05) if t > 0.95 else 0.0
            x_offset = int(-width * (1.0 - title_progress) + width * slide_out)
            title_alpha = int(255 * min(1.0, title_progress))

            # Title text
            try:
                bbox = draw.textbbox((0, 0), title[:50], font=font_title)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(title[:50]) * 20
            tx = max(width // 15, (width - tw) // 2) + x_offset
            ty = card_y + card_h // 4
            draw.text((tx, ty), title[:50], font=font_title, fill=(255, 255, 255, title_alpha))

            # Accent underline wipe
            if 0.15 <= t < 0.75:
                line_progress = ease_out_expo(min(1.0, (t - 0.15) / 0.15))
                line_w = int(min(tw, width * 0.6) * line_progress)
                line_y = ty + (bbox[3] - bbox[1] if 'bbox' in dir() else 40) + 8
                draw.rectangle([(tx, line_y), (tx + line_w, line_y + 4)], fill=(ar, ag, ab, 230))

            # Subtitle fade in
            if 0.20 <= t < 0.75 and subtitle:
                sub_alpha = int(255 * ease_out_expo(min(1.0, (t - 0.20) / 0.10)))
                draw.text((tx, ty + card_h // 2), subtitle[:60], font=font_sub, fill=(200, 200, 200, sub_alpha))

        path = os.path.join(out_dir, f"kt_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


def render_stat_reveal(
    stat_text: str,
    label: str,
    accent_color: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    out_dir: str,
) -> list[str]:
    """Animated number counter with card background."""
    from PIL import Image, ImageDraw
    import re

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(duration * fps)
    paths: list[str] = []
    font_num = load_font(FONT_BOLD, max(60, height // 16))
    font_label = load_font(FONT_LIGHT, max(28, height // 40))

    # Try to parse a number from stat_text
    num_match = re.search(r"[\d,.]+", stat_text.replace(",", ""))
    target_num = float(num_match.group().replace(",", "")) if num_match else 0
    is_pct = "%" in stat_text
    suffix = "%" if is_pct else ""
    prefix = stat_text[:stat_text.find(num_match.group())] if num_match and num_match.start() > 0 else ""

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Card fade
        card_w, card_h = int(width * 0.7), int(height * 0.15)
        cx, cy = (width - card_w) // 2, (height - card_h) // 2
        card_alpha = int(160 * (ease_out_expo(t / 0.05) if t < 0.05 else (1.0 if t < 0.90 else max(0, 1.0 - (t - 0.90) / 0.10))))
        draw.rectangle([(cx, cy), (cx + card_w, cy + card_h)], fill=(0, 0, 0, int(card_alpha)))

        # Number counter
        if t < 0.90:
            count_progress = ease_out_expo(min(1.0, t / 0.40)) if t < 0.40 else 1.0
            current = target_num * count_progress
            if target_num > 1000000:
                display = f"{prefix}{current / 1000000:.1f}M{suffix}"
            elif target_num > 1000:
                display = f"{prefix}{current / 1000:.1f}K{suffix}"
            else:
                display = f"{prefix}{int(current)}{suffix}"
            try:
                bbox = draw.textbbox((0, 0), display, font=font_num)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(display) * 36
            draw.text(((width - tw) // 2, cy + card_h // 6), display, font=font_num, fill=(255, 255, 255, 255))

            # Label fade
            if t >= 0.40:
                label_alpha = int(255 * ease_out_expo(min(1.0, (t - 0.40) / 0.10)))
                try:
                    lbox = draw.textbbox((0, 0), label[:40], font=font_label)
                    lw = lbox[2] - lbox[0]
                except Exception:
                    lw = len(label[:40]) * 16
                draw.text(((width - lw) // 2, cy + card_h * 2 // 3), label[:40], font=font_label, fill=(ar, ag, ab, label_alpha))

        path = os.path.join(out_dir, f"sr_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


def render_quote_scroll(
    quote: str,
    attribution: str,
    accent_color: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    out_dir: str,
) -> list[str]:
    """Word-by-word quote reveal with decorative quotation marks."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(duration * fps)
    paths: list[str] = []
    font_quote = load_font(FONT_LIGHT, max(30, height // 35))
    font_marks = load_font(FONT_BOLD, max(80, height // 14))
    font_attr = load_font(FONT_LIGHT, max(22, height // 50))
    words = quote.split()

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        center_y = height // 2

        # Quotation marks scale in
        if t >= 0.0:
            mark_progress = ease_out_back(min(1.0, t / 0.10))
            mark_alpha = int(200 * mark_progress)
            mark_size = max(80, height // 14)
            mark_font = load_font(FONT_BOLD, int(mark_size * mark_progress))
            if mark_font:
                draw.text((width // 6, center_y - height // 6), "\u201C", font=mark_font, fill=(ar, ag, ab, mark_alpha))
                draw.text((width * 5 // 6 - mark_size, center_y + height // 12), "\u201D", font=mark_font, fill=(ar, ag, ab, mark_alpha))

        # Words reveal one by one
        if 0.10 <= t < 0.95 and words:
            reveal_t = (t - 0.10) / 0.60
            words_to_show = max(1, int(len(words) * min(1.0, reveal_t)))
            visible = " ".join(words[:words_to_show])
            lines = wrap_text(draw, visible, font_quote, int(width * 0.6))
            total_h = len(lines) * (height // 30 + 8)
            y_start = center_y - total_h // 2
            for li, line in enumerate(lines[:6]):
                try:
                    bbox = draw.textbbox((0, 0), line, font=font_quote)
                    lw = bbox[2] - bbox[0]
                except Exception:
                    lw = len(line) * 18
                draw.text(((width - lw) // 2, y_start + li * (height // 30 + 8)), line, font=font_quote, fill=(255, 255, 255, 240))

        # Attribution
        if 0.70 <= t < 0.95 and attribution:
            attr_alpha = int(200 * ease_out_expo(min(1.0, (t - 0.70) / 0.10)))
            draw.text((width // 3, center_y + height // 6), f"— {attribution[:40]}", font=font_attr, fill=(180, 180, 180, attr_alpha))

        # Fade out
        if t >= 0.95:
            pass  # alpha is already handled per-element

        path = os.path.join(out_dir, f"qs_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


def render_lower_third_animated(
    text: str,
    accent_color: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    out_dir: str,
) -> list[str]:
    """Accent bar wipes in from left, text reveals as bar passes."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(duration * fps)
    paths: list[str] = []
    font = load_font(FONT_BOLD, max(30, height // 35))
    bar_h = max(60, height // 18)

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        bar_y = height - bar_h - height // 12

        if t < 0.20:
            # Bar wipe in
            progress = ease_out_expo(t / 0.20)
            bar_w = int(width * 0.8 * progress)
            draw.rectangle([(0, bar_y), (bar_w, bar_y + bar_h)], fill=(0, 0, 0, 200))
            draw.rectangle([(0, bar_y), (min(6, bar_w), bar_y + bar_h)], fill=(ar, ag, ab, 230))
            # Text mask
            text_alpha = int(255 * progress)
            draw.text((50, bar_y + bar_h // 4), text[:50], font=font, fill=(255, 255, 255, text_alpha))
        elif t < 0.75:
            # Hold
            bar_w = int(width * 0.8)
            draw.rectangle([(0, bar_y), (bar_w, bar_y + bar_h)], fill=(0, 0, 0, 200))
            draw.rectangle([(0, bar_y), (6, bar_y + bar_h)], fill=(ar, ag, ab, 230))
            draw.text((50, bar_y + bar_h // 4), text[:50], font=font, fill=(255, 255, 255, 255))
        else:
            # Slide out right
            progress = ease_out_expo((t - 0.75) / 0.20)
            x_off = int(width * progress)
            bar_w = int(width * 0.8)
            draw.rectangle([(x_off, bar_y), (x_off + bar_w, bar_y + bar_h)], fill=(0, 0, 0, 200))
            draw.rectangle([(x_off, bar_y), (x_off + 6, bar_y + bar_h)], fill=(ar, ag, ab, 230))
            draw.text((50 + x_off, bar_y + bar_h // 4), text[:50], font=font, fill=(255, 255, 255, max(0, int(255 * (1 - progress)))))

        path = os.path.join(out_dir, f"lt_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


def render_title_card_full(
    title: str,
    tagline: str,
    accent_color: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    out_dir: str,
    logo_path: str | None = None,
) -> list[str]:
    """Full-screen branded intro card — gradient bg, logo, title, tagline."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(duration * fps)
    paths: list[str] = []
    font_title = load_font(FONT_BOLD, max(44, height // 25))
    font_tag = load_font(FONT_LIGHT, max(26, height // 45))

    # Load logo if provided
    logo_img = None
    if logo_path and os.path.isfile(logo_path):
        try:
            logo_img = Image.open(logo_path).convert("RGBA")
            logo_size = min(width // 4, height // 6)
            logo_img = logo_img.resize((logo_size, logo_size), Image.LANCZOS)
        except Exception:
            logo_img = None

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Gradient background fade from black
        if t >= 0.0:
            bg_alpha = int(220 * ease_in_out_cubic(min(1.0, t / 0.15)))
            if t >= 0.85:
                bg_alpha = int(bg_alpha * max(0, 1.0 - (t - 0.85) / 0.15))
            for row in range(height):
                frac = row / height
                r = int(ar * frac * 0.3)
                g = int(ag * frac * 0.3)
                b = int(ab * frac * 0.3)
                draw.line([(0, row), (width, row)], fill=(r, g, b, bg_alpha))

        # Logo scale in
        if 0.15 <= t < 0.85 and logo_img:
            logo_progress = ease_out_back(min(1.0, (t - 0.15) / 0.15))
            lw = int(logo_img.width * logo_progress)
            lh = int(logo_img.height * logo_progress)
            if lw > 0 and lh > 0:
                scaled = logo_img.resize((lw, lh), Image.LANCZOS)
                lx = (width - lw) // 2
                ly = height // 4 - lh // 2
                img.paste(scaled, (lx, ly), scaled)

        # Title slide up
        if 0.30 <= t < 0.85:
            title_progress = ease_out_expo(min(1.0, (t - 0.30) / 0.15))
            title_alpha = int(255 * title_progress)
            y_off = int(height * 0.05 * (1 - title_progress))
            lines = wrap_text(draw, title[:80], font_title, int(width * 0.8))
            y_start = height // 2 - 20 + y_off
            for li, line in enumerate(lines[:3]):
                try:
                    bbox = draw.textbbox((0, 0), line, font=font_title)
                    lw_ = bbox[2] - bbox[0]
                except Exception:
                    lw_ = len(line) * 26
                draw.text(((width - lw_) // 2, y_start + li * 50), line, font=font_title, fill=(255, 255, 255, title_alpha))

        # Tagline fade in
        if 0.45 <= t < 0.85 and tagline:
            tag_alpha = int(200 * ease_out_expo(min(1.0, (t - 0.45) / 0.10)))
            try:
                bbox = draw.textbbox((0, 0), tagline[:60], font=font_tag)
                tw_ = bbox[2] - bbox[0]
            except Exception:
                tw_ = len(tagline[:60]) * 16
            draw.text(((width - tw_) // 2, height * 2 // 3), tagline[:60], font=font_tag, fill=(200, 200, 200, tag_alpha))

        path = os.path.join(out_dir, f"tc_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


def render_countdown_timer(
    total_duration: float,
    accent_color: str,
    width: int,
    height: int,
    fps: int,
    out_dir: str,
    size: int = 80,
) -> list[str]:
    """Arc depleting clockwise over full video duration — top right corner."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(total_duration * fps)
    paths: list[str] = []
    cx = width - size - 20
    cy = 20

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Dark circle background
        draw.ellipse([(cx, cy), (cx + size, cy + size)], fill=(0, 0, 0, 140))

        # Arc — deplete from 360° to 0°
        remaining_angle = int(360 * (1.0 - t))
        if remaining_angle > 0:
            draw.arc(
                [(cx + 4, cy + 4), (cx + size - 4, cy + size - 4)],
                start=-90, end=-90 + remaining_angle,
                fill=(ar, ag, ab, 230), width=4,
            )

        path = os.path.join(out_dir, f"cd_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


def render_section_transition_card(
    section_title: str,
    accent_color: str,
    width: int,
    height: int,
    duration: float,
    fps: int,
    out_dir: str,
) -> list[str]:
    """Dark overlay + section title scale-in, 1.0s typical."""
    from PIL import Image, ImageDraw

    ar, ag, ab, _ = hex_to_rgba(accent_color)
    total_frames = int(duration * fps)
    paths: list[str] = []
    font = load_font(FONT_BOLD, max(40, height // 28))

    for f in range(total_frames):
        t = f / max(1, total_frames - 1)
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Dark overlay fade
        if t < 0.25:
            overlay_alpha = int(180 * ease_in_out_cubic(t / 0.25))
        elif t < 0.75:
            overlay_alpha = 180
        else:
            overlay_alpha = int(180 * (1.0 - ease_in_out_cubic((t - 0.75) / 0.25)))
        draw.rectangle([(0, 0), (width, height)], fill=(0, 0, 0, overlay_alpha))

        # Title scale in
        if 0.25 <= t < 0.75:
            title_progress = ease_out_back(min(1.0, (t - 0.25) / 0.25))
            title_alpha = int(255 * title_progress)
            try:
                bbox = draw.textbbox((0, 0), section_title[:40], font=font)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw = len(section_title[:40]) * 24
            draw.text(((width - tw) // 2, height // 2 - 20), section_title[:40], font=font, fill=(255, 255, 255, title_alpha))

        path = os.path.join(out_dir, f"st_{f:04d}.png")
        img.save(path)
        paths.append(path)

    return paths


# ── FFmpeg overlay composition helper ────────────────────────────────────────

def build_overlay_filter_from_frames(
    frame_dir: str,
    frame_pattern: str,
    start_time: float,
    duration: float,
    fps: int,
) -> str:
    """Build an FFmpeg filter string to composite a PNG frame sequence as overlay.

    Returns a filter_complex fragment like:
    movie=frame_dir/pattern_%04d.png:loop=0,setpts=PTS-STARTPTS+{start}[ov];[in][ov]overlay=...
    """
    glob = os.path.join(frame_dir, frame_pattern)
    return (
        f"movie='{glob}':loop=0,fps={fps},setpts=PTS-STARTPTS+{start_time}/TB,format=rgba[_ov];"
        f"[in][_ov]overlay=0:0:enable='between(t,{start_time},{start_time + duration})'"
    )

