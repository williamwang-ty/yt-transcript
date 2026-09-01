#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for yt-transcript.
#
# The project's Python code depends only on the standard library plus PyYAML
# (already present in the base image). The external runtime dependencies are the
# yt-dlp and ffmpeg/curl CLIs. ffmpeg and curl ship with the base image, so this
# script installs yt-dlp (and the deno JS runtime it needs for reliable YouTube
# extraction) and prepares a local, gitignored config.yaml.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing/upgrading yt-dlp"
sudo pip3 install --break-system-packages -U yt-dlp

echo "==> Ensuring deno JS runtime is available (used by yt-dlp for YouTube)"
if ! command -v deno >/dev/null 2>&1; then
  export DENO_INSTALL="$HOME/.deno"
  curl -fsSL https://deno.land/install.sh | sh -s -- --no-modify-path
  sudo cp "$DENO_INSTALL/bin/deno" /usr/local/bin/deno
  sudo chmod 755 /usr/local/bin/deno
fi

echo "==> Preparing local config.yaml (gitignored)"
if [ ! -f "$ROOT_DIR/config.yaml" ]; then
  cp "$ROOT_DIR/config.example.yaml" "$ROOT_DIR/config.yaml"
  mkdir -p "$HOME/yt-output"
  sed -i "s|output_dir: \"~/Downloads\"|output_dir: \"$HOME/yt-output\"|" "$ROOT_DIR/config.yaml"
fi

echo "==> Verifying toolchain"
yt-dlp --version
deno --version | head -1
ffmpeg -version | head -1
python3 --version

echo "==> Bootstrap complete"
