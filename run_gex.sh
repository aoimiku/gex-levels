#!/bin/zsh
# 定期実行用（launchd から呼ばれる）。ログは output/run.log に追記。
cd "$(dirname "$0")" || exit 1
mkdir -p output
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  /opt/homebrew/bin/python3 gex_levels.py "$@"
} >> output/run.log 2>&1
