#!/usr/bin/env bash
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "⏹️  Stopping PatchWise Web UI..."
docker compose down
echo "✅ Stopped."
