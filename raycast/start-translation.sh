#!/bin/zsh

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title 日本語訳付き文字起こしを開始
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🌐
# @raycast.packageName Live Transcribe
# @raycast.description 専用Terminalタブで日本語訳付き文字起こしを開始、または実行中のタブを表示します

set -euo pipefail

exec "${0:A:h}/start-transcription.sh" --translate-ja
