#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 "$task_root/scripts/package.py" --stage-only
codex plugin marketplace add "$task_root" --json
task_installation=$(codex plugin add ios-use@ios-use-local --json)
printf '%s\n' "$task_installation" | python3 "$task_root/scripts/register_mcp.py"
printf '%s\n' 'Reconnect the Codex chat to load iOS Use tools and skills.'
