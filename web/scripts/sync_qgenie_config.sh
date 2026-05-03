#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SRC_DIR="${QGENIE_HOST_CONFIG_DIR:-$HOME/.config/qgenie-cli}"
DST_DIR="${QGENIE_RUNTIME_CONFIG_DIR:-$REPO_ROOT/.runtime/qgenie-cli}"

if [[ ! -f "$SRC_DIR/config.toml" ]]; then
  echo "❌ Missing QGenie config: $SRC_DIR/config.toml"
  echo "Run qgenie setup on host first, then retry."
  exit 1
fi

mkdir -p "$DST_DIR"
install -m 0600 "$SRC_DIR/config.toml" "$DST_DIR/config.toml"

if [[ -f "$SRC_DIR/update-cache.json" ]]; then
  install -m 0600 "$SRC_DIR/update-cache.json" "$DST_DIR/update-cache.json"
fi

if [[ -d "$SRC_DIR/prompts" ]]; then
  rm -rf "$DST_DIR/prompts"
  mkdir -p "$DST_DIR/prompts"
  cp -a "$SRC_DIR/prompts/." "$DST_DIR/prompts/"
fi

if [[ -f "$SRC_DIR/agent/config.toml" ]]; then
  mkdir -p "$DST_DIR/agent"
  install -m 0600 "$SRC_DIR/agent/config.toml" "$DST_DIR/agent/config.toml"
fi

echo "✅ Synced QGenie config for Docker runtime:"
echo "   source: $SRC_DIR"
echo "   target: $DST_DIR"
