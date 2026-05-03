#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(dirname "$SCRIPT_DIR")"

echo "🚀 Starting PatchWise Web UI..."
echo "📦 Building Docker image..."
cd "$WEB_DIR"
docker compose up -d --build

echo ""
echo "✅ PatchWise Web UI is running!"
echo ""
echo "👉 To access from your laptop:"
echo "   ssh -L 7788:localhost:7788 nandam@hu-nandam-hyd"
echo "   Then open: http://localhost:7788"
echo ""
echo "📺 To view screen sessions manually:"
echo "   screen -ls"
echo ""
echo "📋 To view logs:"
echo "   docker compose logs -f patchwise-web"
