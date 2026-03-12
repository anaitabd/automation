"""Render overlay frame sequences using shared motion library."""

from __future__ import annotations

import os
import sys

# Import shared motion library
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
import motion

from config import OUTPUT_FPS, OUTPUT_HEIGHT, OUTPUT_WIDTH


def render_overlay(
    overlay_type: str,
    text: str,
    accent_color: str,
    duration: float,
    out_dir: str,
    subtitle: str = "",
    label: str = "",
    attribution: str = "",
    total_duration: float = 0.0,
    logo_path: str | None = None,
) -> list[str]:
    """Render overlay frame sequence. Returns list of PNG paths.

    Dispatches to the appropriate renderer from the shared motion library.
    All outputs are 1080×1920 vertical PNG frames.
    """
    os.makedirs(out_dir, exist_ok=True)

    if overlay_type == "kinetic_title":
        return motion.render_kinetic_title(
            title=text, subtitle=subtitle, accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            duration=duration, fps=OUTPUT_FPS, out_dir=out_dir,
        )
    elif overlay_type == "stat_reveal":
        return motion.render_stat_reveal(
            stat_text=text, label=label, accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            duration=duration, fps=OUTPUT_FPS, out_dir=out_dir,
        )
    elif overlay_type == "quote_scroll":
        return motion.render_quote_scroll(
            quote=text, attribution=attribution, accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            duration=duration, fps=OUTPUT_FPS, out_dir=out_dir,
        )
    elif overlay_type == "lower_third_animated":
        return motion.render_lower_third_animated(
            text=text, accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            duration=duration, fps=OUTPUT_FPS, out_dir=out_dir,
        )
    elif overlay_type == "title_card_full":
        return motion.render_title_card_full(
            title=text, tagline=subtitle, accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            duration=duration, fps=OUTPUT_FPS, out_dir=out_dir,
            logo_path=logo_path,
        )
    elif overlay_type == "countdown_timer":
        return motion.render_countdown_timer(
            total_duration=total_duration or duration,
            accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            fps=OUTPUT_FPS, out_dir=out_dir,
        )
    elif overlay_type == "section_transition_card":
        return motion.render_section_transition_card(
            section_title=text, accent_color=accent_color,
            width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
            duration=duration, fps=OUTPUT_FPS, out_dir=out_dir,
        )
    else:
        return []

