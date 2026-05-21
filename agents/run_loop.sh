#!/bin/bash
# Auto-improvement loop: runs the Claude Code CLI headlessly, one task per iteration.
#
# Prerequisites:
#   npm install -g @anthropic-ai/claude-code
#
# Usage:
#   cd <repo-root>
#   bash agents/run_loop.sh
#   bash agents/run_loop.sh 10   # override iteration count

set -u

REPO_ROOT="$(git rev-parse --show-toplevel)"
MAX_ITERATIONS="${1:-5}"
PROMPT_FILE="$REPO_ROOT/agents/improvement_prompt.md"
RUNS_DIR="$REPO_ROOT/.agent/runs"
TARGET_BRANCH="auto-improve"

mkdir -p "$RUNS_DIR"

# ── guard: must be run from repo root ──────────────────────────────────────────
cd "$REPO_ROOT"

if [ ! -f "$PROMPT_FILE" ]; then
  echo "ERROR: prompt file not found at $PROMPT_FILE"
  exit 1
fi

# ── branch setup ──────────────────────────────────────────────────────────────
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
  echo "On $CURRENT_BRANCH — switching to $TARGET_BRANCH"
  if git show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git checkout "$TARGET_BRANCH"
  else
    git checkout -b "$TARGET_BRANCH"
  fi
  CURRENT_BRANCH="$TARGET_BRANCH"
fi

echo "Starting loop on branch: $CURRENT_BRANCH"
echo "Max iterations: $MAX_ITERATIONS"
echo "Prompt: $PROMPT_FILE"
echo

# ── main loop ─────────────────────────────────────────────────────────────────
for i in $(seq 1 "$MAX_ITERATIONS"); do
  echo "================================="
  echo "  Iteration $i / $MAX_ITERATIONS"
  echo "================================="

  OUTPUT_FILE="$RUNS_DIR/iter_${i}.json"

  # Run Claude Code headless (-p = print/non-interactive)
  claude -p "$(cat "$PROMPT_FILE")" \
    --output-format json \
    --allowedTools "Bash,Read,Edit,Write" \
    > "$OUTPUT_FILE" 2>&1

  EXIT_CODE=$?
  if [ $EXIT_CODE -ne 0 ]; then
    echo "WARNING: claude exited with code $EXIT_CODE on iteration $i"
  fi

  # Show cost / duration summary
  if command -v jq >/dev/null 2>&1; then
    echo "--- Iteration $i summary ---"
    jq -r '"Cost: $\(.total_cost_usd // "?")  Duration: \(.duration_ms // "?")ms  Turns: \(.num_turns // "?")"' \
      "$OUTPUT_FILE" 2>/dev/null \
      || echo "(jq parse failed — see $OUTPUT_FILE)"
  fi

  # Check for the sentinel the agent is required to emit
  if grep -q "IMPROVEMENT_COMPLETE" "$OUTPUT_FILE" 2>/dev/null; then
    echo "Sentinel found — iteration $i completed cleanly."
  else
    echo "WARNING: IMPROVEMENT_COMPLETE not found in output — agent may have failed."
  fi

  # Commit whatever changed
  git add -A
  if ! git diff --staged --quiet; then
    git commit -m "auto: iteration $i" --no-verify
    echo "Committed changes from iteration $i"
  else
    echo "No file changes in iteration $i"
  fi

  echo
done

echo "================================="
echo "  Done. $MAX_ITERATIONS iterations complete."
echo "================================="
echo "Review with:  git log --oneline $TARGET_BRANCH"
echo "Per-run logs: $RUNS_DIR/"
if command -v jq >/dev/null 2>&1; then
  echo "Total cost:   \$(jq -s '[.[].total_cost_usd // 0] | add' $RUNS_DIR/*.json)"
fi
