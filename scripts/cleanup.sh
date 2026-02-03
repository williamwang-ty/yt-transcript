#!/usr/bin/env bash

# cleanup.sh - Clean up temporary files
# Usage: ./cleanup.sh <VIDEO_ID>

if [ $# -lt 1 ]; then
    echo "Usage: $0 <VIDEO_ID>"
    exit 1
fi

VIDEO_ID="$1"

echo "🧹 Cleaning up temporary files for video: $VIDEO_ID"

# Remove all temp files related to this video
rm -f /tmp/${VIDEO_ID}.*
rm -f /tmp/${VIDEO_ID}_*.json
rm -f /tmp/${VIDEO_ID}_chunk_*
rm -f /tmp/${VIDEO_ID}_raw_text.txt
rm -f /tmp/${VIDEO_ID}_structured.txt
rm -f /tmp/${VIDEO_ID}_optimized.txt
rm -f /tmp/${VIDEO_ID}_combined_transcript.txt
rm -rf /tmp/${VIDEO_ID}_chunks/

echo "✅ Cleanup complete"
