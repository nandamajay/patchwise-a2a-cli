#!/usr/bin/env bash
# Run without Docker for development
cd "$(dirname "${BASH_SOURCE[0]}")/.."
pip install -r requirements.txt
cd backend
A2A_ROOT=/local/mnt/workspace/A2A_CLI \
PATCHES_ROOT=/local/mnt/workspace/upstream_patches \
uvicorn main:app --host 0.0.0.0 --port 7788 --reload
