#!/bin/bash
# Auto-improvement loop: runs the Claude Code CLI headlessly, one task per iteration.
#
# Each iteration the agent picks the top task from TODO.md, implements it, runs the
# test suite, and commits. The loop stops when ANY budget limit is hit first.
#
# Prerequisites:
#   npm install -g @anthropic-ai/claude-code   # provides the `claude` CLI
#   jq                                         # JSON parsing (brew install jq)
#
# Usage:
#   bash agents/run_loop.sh                       # defaults: $5 budget, 60 min, 20 iters
#   bash agents/run_loop.sh --budget 2.50         # stop once spend passes $2.50
#   bash agents/run_loop.sh --minutes 30          # stop after 30 wall-clock minutes
#   bash agents/run_loop.sh --max-iter 8          # stop after 8 iterations
#   bash agents/run_loop.sh --budget 10 --minutes 90
#
# Whichever limit is reached first ends the loop.

set -u

# ── defaults (override via flags) ───────────────────────────────────────────────
MAX_COST_USD="5.00"    # cumulative spend cap (the primary control)
MAX_MINUTES="60"       # wall-clock cap
MAX_ITERATIONS="20"    # hard safety cap on iteration count

while [ $# -gt 0 ]; do
  case "$1" in
    --budget)   MAX_COST_USD="$2"; shift 2 ;;
    --minutes)  MAX_MINUTES="$2";  shift 2 ;;
    --max-iter) MAX_ITERATIONS="$2"; shift 2 ;;
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0 ;;
    *)
      echo "Unknown argument: $1 (use --help)"; exit 1 ;;
  esac
done

# ── setup ───────────────────────────────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROMPT_FILE="$REPO_ROOT/agents/improvement_prompt.md"
RUNS_DIR="$REPO_ROOT/.agent/runs"
TARGET_BRANCH="auto-improve"

cd "$REPO_ROOT"
mkdir -p "$RUNS_DIR"

for tool in claude jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' not found on PATH."; exit 1; }
done
[ -f "$PROMPT_FILE" ] || { echo "ERROR: prompt file not found at $PROMPT_FILE"; exit 1; }

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

# ── helpers ───────────────────────────────────────────────────────────────────
fmt_duration() { # ms -> "Xm Ys"
  local ms="$1" s; s=$(( ms / 1000 ))
  printf '%dm %02ds' $(( s / 60 )) $(( s % 60 ))
}

human_tokens() { # 12345 -> 12.3k
  awk -v n="$1" 'BEGIN { if (n>=1000) printf "%.1fk", n/1000; else printf "%d", n }'
}

# ── banner ────────────────────────────────────────────────────────────────────
START_EPOCH=$(date +%s)
TOTAL_COST="0"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Auto-improvement loop                                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Branch         : $CURRENT_BRANCH"
echo "  Budget cap     : \$$MAX_COST_USD"
echo "  Time cap       : ${MAX_MINUTES} min"
echo "  Iteration cap  : $MAX_ITERATIONS"
echo "  Prompt         : ${PROMPT_FILE#$REPO_ROOT/}"
echo "  Logs           : ${RUNS_DIR#$REPO_ROOT/}/"
echo

# ── main loop ─────────────────────────────────────────────────────────────────
i=0
while [ "$i" -lt "$MAX_ITERATIONS" ]; do
  i=$(( i + 1 ))
  ITER_START=$(date +%s)
  HEAD_BEFORE="$(git rev-parse HEAD)"
  OUTPUT_FILE="$RUNS_DIR/iter_${i}.json"

  ELAPSED_MIN=$(( (ITER_START - START_EPOCH) / 60 ))
  echo "─────────────────────────────────────────────────────────────────"
  printf "  Iteration %d/%d   ·   spent \$%s/%s   ·   %dm/%smin elapsed\n" \
    "$i" "$MAX_ITERATIONS" "$TOTAL_COST" "$MAX_COST_USD" "$ELAPSED_MIN" "$MAX_MINUTES"
  echo "─────────────────────────────────────────────────────────────────"
  echo "  Working… (asking the agent to pick + implement one task)"

  # Run Claude Code headless (-p = print/non-interactive).
  claude -p "$(cat "$PROMPT_FILE")" \
    --output-format json \
    --allowedTools "Bash,Read,Edit,Write" \
    > "$OUTPUT_FILE" 2>>"$RUNS_DIR/iter_${i}.stderr"

  CLAUDE_EXIT=$?

  # ── parse the run ────────────────────────────────────────────────────────────
  if ! jq -e . "$OUTPUT_FILE" >/dev/null 2>&1; then
    echo "  ⚠  Output was not valid JSON (claude exit=$CLAUDE_EXIT). See $OUTPUT_FILE"
    echo
    continue
  fi

  COST=$(jq -r '.total_cost_usd // 0' "$OUTPUT_FILE")
  DUR_MS=$(jq -r '.duration_ms // 0' "$OUTPUT_FILE")
  TURNS=$(jq -r '.num_turns // 0' "$OUTPUT_FILE")
  IS_ERROR=$(jq -r '.is_error // false' "$OUTPUT_FILE")
  IN_TOK=$(jq -r '.usage.input_tokens // 0' "$OUTPUT_FILE")
  OUT_TOK=$(jq -r '.usage.output_tokens // 0' "$OUTPUT_FILE")
  CACHE_TOK=$(jq -r '.usage.cache_read_input_tokens // 0' "$OUTPUT_FILE")
  RESULT_TEXT=$(jq -r '.result // ""' "$OUTPUT_FILE")

  TOTAL_COST=$(awk -v a="$TOTAL_COST" -v b="$COST" 'BEGIN { printf "%.4f", a + b }')

  # ── what changed this iteration ────────────────────────────────────────────────
  git add -A
  CHANGED_STAT="$(git diff --staged --stat | tail -n 1)"
  CHANGED_FILES="$(git diff --staged --name-only | sed 's/^/      /')"

  # ── report ─────────────────────────────────────────────────────────────────────
  echo
  if [ "$IS_ERROR" = "true" ] || [ "$CLAUDE_EXIT" -ne 0 ]; then
    echo "  Status   : ⚠  ERROR (claude exit=$CLAUDE_EXIT, is_error=$IS_ERROR)"
  elif grep -q "IMPROVEMENT_COMPLETE" "$OUTPUT_FILE" 2>/dev/null; then
    echo "  Status   : ✓ complete"
  else
    echo "  Status   : ⚠  finished without IMPROVEMENT_COMPLETE sentinel"
  fi

  echo "  Cost     : \$$COST  (cumulative \$$TOTAL_COST)"
  echo "  Time     : $(fmt_duration "$DUR_MS")   ·   $TURNS turns"
  echo "  Tokens   : $(human_tokens "$IN_TOK") in / $(human_tokens "$OUT_TOK") out / $(human_tokens "$CACHE_TOK") cached"

  if [ -n "$CHANGED_FILES" ]; then
    echo "  Modified :"
    echo "$CHANGED_FILES"
    [ -n "$CHANGED_STAT" ] && echo "     ($(echo "$CHANGED_STAT" | sed 's/^ *//'))"
  else
    echo "  Modified : (no file changes)"
  fi

  # Agent's own summary of what it did — last meaningful lines of its final message.
  if [ -n "$RESULT_TEXT" ]; then
    echo "  Summary  :"
    echo "$RESULT_TEXT" | grep -v '^[[:space:]]*$' | tail -n 4 | sed 's/^/      /'
  fi
  echo

  # ── commit ───────────────────────────────────────────────────────────────────
  if ! git diff --staged --quiet; then
    SHORT_SUMMARY="$(echo "$RESULT_TEXT" | grep -v '^[[:space:]]*$' | head -n 1 | cut -c1-72)"
    [ -z "$SHORT_SUMMARY" ] && SHORT_SUMMARY="iteration $i"
    git commit -m "auto(iter $i): $SHORT_SUMMARY" --no-verify >/dev/null
    echo "  Committed: $(git rev-parse --short HEAD)"
  else
    echo "  Committed: (nothing to commit)"
  fi
  echo

  # ── budget checks (evaluated AFTER the iteration so the cap is a ceiling) ──────
  ELAPSED_MIN=$(( ($(date +%s) - START_EPOCH) / 60 ))

  if awk -v c="$TOTAL_COST" -v m="$MAX_COST_USD" 'BEGIN { exit !(c >= m) }'; then
    echo "▸ Budget cap reached: \$$TOTAL_COST ≥ \$$MAX_COST_USD. Stopping."
    break
  fi
  if [ "$ELAPSED_MIN" -ge "$MAX_MINUTES" ]; then
    echo "▸ Time cap reached: ${ELAPSED_MIN}min ≥ ${MAX_MINUTES}min. Stopping."
    break
  fi
done

# ── final summary ───────────────────────────────────────────────────────────────
TOTAL_MIN=$(( ($(date +%s) - START_EPOCH) / 60 ))
echo "═════════════════════════════════════════════════════════════════"
echo "  Loop finished"
echo "═════════════════════════════════════════════════════════════════"
echo "  Iterations run : $i"
echo "  Total spend    : \$$TOTAL_COST"
echo "  Total time     : ${TOTAL_MIN} min"
echo "  Commits        : $(git rev-list --count "$HEAD_BEFORE"..HEAD 2>/dev/null || echo '?') new on $CURRENT_BRANCH"
echo
echo "  Review changes : git log --oneline ${CURRENT_BRANCH}"
echo "  Per-run logs   : ${RUNS_DIR#$REPO_ROOT/}/"
