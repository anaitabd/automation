#!/usr/bin/env python3

import argparse
import os
import sys

try:
    import boto3
except ImportError:
    boto3 = None

LUT_SIZE = 33
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "luts_generated")
S3_PREFIX = "luts/"


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def _apply_gain_gamma(r: float, g: float, b: float, gain=(1,1,1), gamma=(1,1,1)) -> tuple:
    r = _clamp(r ** (1 / gamma[0]) * gain[0])
    g = _clamp(g ** (1 / gamma[1]) * gain[1])
    b = _clamp(b ** (1 / gamma[2]) * gain[2])
    return r, g, b


def _mix(a: float, b: float, t: float) -> float:
    return a * (1 - t) + b * t


def _transform_cinematic_warm(r, g, b):
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    shadow = _clamp(1.0 - lum * 3)
    r -= shadow * 0.04
    g += shadow * 0.02
    b += shadow * 0.06
    highlight = _clamp((lum - 0.6) * 2.5)
    r += highlight * 0.08
    g += highlight * 0.04
    b -= highlight * 0.06
    r = _clamp(r * 1.04)
    g = _clamp(g * 0.99)
    b = _clamp(b * 0.95)
    return _clamp(r), _clamp(g), _clamp(b)


def _transform_cold_blue(r, g, b):
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    r = _mix(lum, r, 0.80)
    g = _mix(lum, g, 0.80)
    b = _mix(lum, b, 0.80)
    r = _clamp(r * 0.94)
    b = _clamp(b * 1.06)
    r = _clamp(r * 0.92 + 0.04)
    g = _clamp(g * 0.92 + 0.04)
    b = _clamp(b * 0.92 + 0.06)
    return r, g, b


def _transform_punchy_vibrant(r, g, b):
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    r = _clamp(_mix(lum, r, 1.4))
    g = _clamp(_mix(lum, g, 1.4))
    b = _clamp(_mix(lum, b, 1.4))
    r = _clamp(r * 1.05)
    b = _clamp(b * 0.96)
    r = _clamp(r ** 0.9)
    g = _clamp(g ** 0.9)
    b = _clamp(b ** 0.9)
    return r, g, b


def _transform_vintage_sepia(r, g, b):
    ro = _clamp(r * 0.393 + g * 0.769 + b * 0.189)
    go = _clamp(r * 0.349 + g * 0.686 + b * 0.168)
    bo = _clamp(r * 0.272 + g * 0.534 + b * 0.131)
    r = _mix(r, ro, 0.70)
    g = _mix(g, go, 0.70)
    b = _mix(b, bo, 0.70)
    r = _clamp(r * 0.85 + 0.06)
    g = _clamp(g * 0.82 + 0.05)
    b = _clamp(b * 0.75 + 0.03)
    return r, g, b


def _transform_high_contrast(r, g, b):
    def s_curve(v):
        v = _clamp(v)
        return v * v * (3 - 2 * v)

    r = _mix(r, s_curve(r), 0.60)
    g = _mix(g, s_curve(g), 0.60)
    b = _mix(b, s_curve(b), 0.60)
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    r = _clamp(_mix(lum, r, 0.92))
    g = _clamp(_mix(lum, g, 0.92))
    b = _clamp(_mix(lum, b, 0.92))
    return r, g, b


LUTS = {
    "cinematic_teal_orange.cube": _transform_cinematic_warm,
    "cold_blue_corporate.cube": _transform_cold_blue,
    "punchy_vibrant_warm.cube": _transform_punchy_vibrant,
    "vintage_sepia.cube": _transform_vintage_sepia,
    "high_contrast.cube": _transform_high_contrast,
}


def _write_cube(filepath: str, transform_fn, lut_size: int = LUT_SIZE) -> None:
    title = os.path.splitext(os.path.basename(filepath))[0]
    step = 1.0 / (lut_size - 1)
    with open(filepath, "w") as f:
        f.write(f"TITLE \"{title}\"\n")
        f.write(f"LUT_3D_SIZE {lut_size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        f.write("\n")
        for bi in range(lut_size):
            for gi in range(lut_size):
                for ri in range(lut_size):
                    r = ri * step
                    g = gi * step
                    b = bi * step
                    ro, go, bo = transform_fn(r, g, b)
                    f.write(f"{ro:.6f} {go:.6f} {bo:.6f}\n")

    print(f"  ✔ Generated {filepath}")


def generate_all(output_dir: str) -> list[str]:
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    for filename, fn in LUTS.items():
        out_path = os.path.join(output_dir, filename)
        _write_cube(out_path, fn)
        paths.append(out_path)
    return paths


def upload_to_s3(paths: list[str], bucket: str) -> None:
    if boto3 is None:
        print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
        sys.exit(1)
    s3 = boto3.client("s3")
    for local_path in paths:
        key = S3_PREFIX + os.path.basename(local_path)
        print(f"  ⬆ Uploading s3://{bucket}/{key} …", end=" ")
        s3.upload_file(local_path, bucket, key)
        print("done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate and optionally upload .cube LUT files")
    parser.add_argument("--upload-to-s3", action="store_true", help="Upload to S3 after generation")
    parser.add_argument("--bucket", default="nexus-assets", help="S3 bucket name (default: nexus-assets)")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Local output directory")
    args = parser.parse_args()

    print(f"\nGenerating {len(LUTS)} LUT files → {args.output_dir}\n")
    paths = generate_all(args.output_dir)

    if args.upload_to_s3:
        print(f"\nUploading to s3://{args.bucket}/{S3_PREFIX}\n")
        upload_to_s3(paths, args.bucket)

    print(f"\n✅ Done. {len(paths)} LUT files ready.\n")
