#!/bin/zsh

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title 文字起こしを開始
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🎙️
# @raycast.packageName Live Transcribe
# @raycast.description 専用Terminalタブで文字起こしを開始、または実行中のタブを表示します

set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_DIR=${SCRIPT_DIR:h}

if [[ ${1:-} == "--run" ]]; then
  shift
  if (( $# > 1 )) || [[ ${1:-} != "" && ${1:-} != "--translate-ja" ]]; then
    print -u2 "使い方: $0 --run [--translate-ja]"
    exit 2
  fi

  cd "$PROJECT_DIR"
  set -a
  if [[ -f .env.local ]]; then
    source .env.local
  fi
  set +a
  exec uv run transcribe.py --device "${TRANSCRIBE_DEVICE:-0}" "$@"
fi

if (( $# > 1 )) || [[ ${1:-} != "" && ${1:-} != "--translate-ja" ]]; then
  print -u2 "使い方: $0 [--translate-ja]"
  exit 2
fi

runtime_command=("$SCRIPT_DIR/start-transcription.sh" --run "$@")
terminal_command=""
for argument in "${runtime_command[@]}"; do
  printf -v quoted_argument "%q" "$argument"
  terminal_command+="${terminal_command:+ }${quoted_argument}"
done

/usr/bin/osascript "$SCRIPT_DIR/reuse-terminal.applescript" "$terminal_command"
