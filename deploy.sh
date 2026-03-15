#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════╗
# ║  Nexus Cloud — Deployment Entry Point                       ║
# ║  Delegates to terraform/scripts/deploy_tf.sh                ║
# ╚══════════════════════════════════════════════════════════════╝

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/terraform/scripts/deploy_tf.sh" "$@"

