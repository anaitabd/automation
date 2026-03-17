"""Configuration — tier definitions, LUT presets, constants."""

from __future__ import annotations

import os

# ── Environment variables ────────────────────────────────────────────────────

SHORTS_ENABLED = os.environ.get("SHORTS_ENABLED", "true").lower() == "true"
SHORTS_TIERS = os.environ.get("SHORTS_TIERS", "micro,short,mid,full").split(",")
SHORTS_MAX_WORKERS = int(os.environ.get("SHORTS_MAX_WORKERS", "3"))
NOVA_REEL_SHORTS_BUDGET = int(os.environ.get("NOVA_REEL_SHORTS_BUDGET", "6"))
SHORTS_LOOP_VERIFY = os.environ.get("SHORTS_LOOP_VERIFY", "true").lower() == "true"
SHORTS_LOOP_THRESHOLD = float(os.environ.get("SHORTS_LOOP_THRESHOLD", "0.85"))
SHORTS_OUTPUT_PREFIX = os.environ.get("SHORTS_OUTPUT_PREFIX", "shorts/")

SCRATCH_DIR = os.environ.get("TMPDIR", "/mnt/scratch")

S3_ASSETS_BUCKET = os.environ.get("ASSETS_BUCKET", "nexus-assets")
S3_OUTPUTS_BUCKET = os.environ.get("OUTPUTS_BUCKET", "nexus-outputs")
S3_CONFIG_BUCKET = os.environ.get("CONFIG_BUCKET", "nexus-config")

# ── Duration tiers ───────────────────────────────────────────────────────────

TIER_DEFS: dict[str, dict] = {
    "micro": {"duration": 15.0, "sections_min": 1, "sections_max": 1, "nova_clips": 2},
    "short": {"duration": 30.0, "sections_min": 2, "sections_max": 3, "nova_clips": 4},
    "mid":   {"duration": 45.0, "sections_min": 3, "sections_max": 4, "nova_clips": 5},
    "full":  {"duration": 60.0, "sections_min": 4, "sections_max": 6, "nova_clips": 6},
}

# ── LUT presets (profile name → LUT .cube key in ASSETS_BUCKET) ─────────────

LUT_PRESETS: dict[str, str] = {
    "teal_orange":    "luts/cinematic_teal_orange.cube",
    "cold_blue":      "luts/cold_blue_corporate.cube",
    "warm_gold":      "luts/cinematic_teal_orange.cube",
    "clean_neutral":  "luts/high_contrast.cube",
    "dark_cinematic": "luts/punchy_vibrant_warm.cube",
}

# ── BPM defaults (fallback if profile.shorts.bpm_estimate missing) ──────────

BPM_DEFAULTS: dict[str, int] = {
    "documentary": 75,
    "finance": 95,
    "entertainment": 120,
    "true_crime": 65,
}

# ── Output specs ─────────────────────────────────────────────────────────────

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_FPS = 30
OUTPUT_CRF = 18
OUTPUT_AUDIO_BITRATE = "192k"
TARGET_LUFS = -14
TRUE_PEAK_LIMIT = -1.0

# ── FFmpeg binary discovery ──────────────────────────────────────────────────

def find_bin(name: str) -> str:
    for candidate in (f"/opt/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    import shutil
    path = shutil.which(name)
    if path:
        return path
    raise FileNotFoundError(f"{name} not found")


FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or find_bin("ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN") or find_bin("ffprobe")

