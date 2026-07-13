#!/usr/bin/env bash
# PostToolUse guardrail: after any edit to the compliance/behavior planes
# (server/app/orchestrator/ or server/skills/), re-run the text evals. If they go
# red, exit 2 so the failure is fed back before proceeding. No-op for other files.
set -u

payload="$(cat)"

# Repo root = parent of this script's dir.
here="$(cd "$(dirname "$0")/.." && pwd)"
server="$here/server"
runner="$server/evals/run_text.py"
[ -f "$runner" ] || exit 0

py="$server/.venv/Scripts/python.exe"
[ -x "$py" ] || py="python"

# Extract the edited file path from the hook payload (best-effort).
file_path="$(printf '%s' "$payload" \
  | "$py" -c 'import sys,json;
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null || true)"

case "$file_path" in
  *server/app/orchestrator/*|*server/skills/*) ;;
  *) exit 0 ;;
esac

cd "$server" || exit 0
out="$("$py" evals/run_text.py --all 2>&1)"
if [ $? -ne 0 ]; then
  echo "EVALS RED after editing $file_path" >&2
  printf '%s\n' "$out" | tail -20 >&2
  exit 2
fi
exit 0
