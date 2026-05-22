#!/bin/bash
# Auto-improvement loop: runs the Claude Code CLI headlessly, one task per iteration.
#
# Each iteration the agent picks the top task from TODO.md, implements it, runs the
# test suite, and commits. The loop stops when a stopping condition is reached.
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
#   bash agents/run_loop.sh --until-done          # run until session limit OR all work done
#
# In normal mode the loop stops when ANY cap (budget / time / iterations) is hit first.
#
# --until-done mode removes the budget/time/iteration caps and instead runs until:
#   • the Claude session hits its usage limit (detected from the CLI output), OR
#   • the agent reports there is nothing left to do (ALL_TASKS_COMPLETE sentinel), OR
#   • a safety guard trips (repeated errors, or no changes for several iterations).
# You may still pass --budget / --minutes alongside --until-done as extra ceilings.

set -u

# ── defaults (override via flags) ───────────────────────────────────────────────
MAX_COST_USD="5.00"    # cumulative spend cap (the primary control)
MAX_MINUTES="60"       # wall-clock cap
MAX_ITERATIONS="20"    # hard safety cap on iteration count
RUN_UNTIL_DONE=0

USER_SET_BUDGET=0; USER_SET_MINUTES=0; USER_SET_MAXITER=0

# Safety guards for --until-done (so a stuck/broken state can't burn the session)
MAX_CONSEC_ERRORS=3      # stop after this many consecutive claude errors
MAX_CONSEC_NOCHANGE=3    # stop after this many consecutive no-change iterations

while [ $# -gt 0 ]; do
  case "$1" in
    --budget)     MAX_COST_USD="$2"; USER_SET_BUDGET=1; shift 2 ;;
    --minutes)    MAX_MINUTES="$2";  USER_SET_MINUTES=1; shift 2 ;;
    --max-iter)   MAX_ITERATIONS="$2"; USER_SET_MAXITER=1; shift 2 ;;
    --until-done) RUN_UNTIL_DONE=1; shift ;;
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0 ;;
    *)
      echo "Unknown argument: $1 (use --help)"; exit 1 ;;
  esac
done

# In until-done mode, disable any cap the user did not explicitly set.
if [ "$RUN_UNTIL_DONE" -eq 1 ]; then
  [ "$USER_SET_BUDGET" -eq 0 ] && MAX_COST_USD=""      # "" = no budget cap
  [ "$USER_SET_MINUTES" -eq 0 ] && MAX_MINUTES=""      # "" = no time cap
  [ "$USER_SET_MAXITER" -eq 0 ] && MAX_ITERATIONS=10000  # generous safety ceiling
fi

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

# Detect a Claude session usage / rate limit from the run's stdout + stderr.
detect_usage_limit() { # $1 = json file, $2 = stderr file
  grep -qiE 'usage limit|rate.?limit|429|too many requests|quota|limit reached|limit will reset|upgrade to (increase|a higher)' \
    "$1" "$2" 2>/dev/null
}

# ── banner ────────────────────────────────────────────────────────────────────
START_EPOCH=$(date +%s)
TOTAL_COST="0"
CONSEC_ERRORS=0
CONSEC_NOCHANGE=0
STOP_REASON=""

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Auto-improvement loop                                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo "  Branch         : $CURRENT_BRANCH"
if [ "$RUN_UNTIL_DONE" -eq 1 ]; then
  echo "  Mode           : run until session limit OR all work complete"
  [ -n "$MAX_COST_USD" ] && echo "  Budget ceiling : \$$MAX_COST_USD"
  [ -n "$MAX_MINUTES" ]  && echo "  Time ceiling   : ${MAX_MINUTES} min"
  echo "  Safety guards  : stop after $MAX_CONSEC_ERRORS consecutive errors or $MAX_CONSEC_NOCHANGE no-change iterations"
else
  echo "  Mode           : stop at first cap"
  echo "  Budget cap     : \$$MAX_COST_USD"
  echo "  Time cap       : ${MAX_MINUTES} min"
  echo "  Iteration cap  : $MAX_ITERATIONS"
fi
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
  STDERR_FILE="$RUNS_DIR/iter_${i}.stderr"
  : > "$STDERR_FILE"

  ELAPSED_MIN=$(( (ITER_START - START_EPOCH) / 60 ))
  echo "─────────────────────────────────────────────────────────────────"
  printf "  Iteration %d   ·   spent \$%s   ·   %dm elapsed\n" "$i" "$TOTAL_COST" "$ELAPSED_MIN"
  echo "─────────────────────────────────────────────────────────────────"
  echo "  Working… (asking the agent to pick + implement one task)"

  # Run Claude Code headless (-p = print/non-interactive).
  claude -p "$(cat "$PROMPT_FILE")" \
    --output-format json \
    --allowedTools "Bash,Read,Edit,Write" \
    > "$OUTPUT_FILE" 2>>"$STDERR_FILE"

  CLAUDE_EXIT=$?

  # ── usage-limit check ──────────────────────────────────────────────────────────
  # Only treat limit-language as a real session limit when the run ALSO errored
  # (non-zero exit or is_error:true). This avoids false-positives when the agent's
  # own summary mentions "rate limit" / "usage limit" on a successful run — likely
  # in THIS repo, which is all about rate limits and cost control. Determined here
  # (not after the JSON parse) because a real limit may emit no/invalid JSON.
  IS_ERROR_RAW="$(jq -r '.is_error // empty' "$OUTPUT_FILE" 2>/dev/null)"
  RUN_ERRORED=0
  { [ "$CLAUDE_EXIT" -ne 0 ] || [ "$IS_ERROR_RAW" = "true" ]; } && RUN_ERRORED=1
  if [ "$RUN_ERRORED" -eq 1 ] && detect_usage_limit "$OUTPUT_FILE" "$STDERR_FILE"; then
    echo
    echo "  Status   : ⛔ session usage limit reached"
    LIMIT_MSG="$(grep -hioE '[^.]*limit[^.]*' "$OUTPUT_FILE" "$STDERR_FILE" 2>/dev/null | head -n 1 | sed 's/^[[:space:]]*//')"
    [ -n "$LIMIT_MSG" ] && echo "  Detail   : $LIMIT_MSG"
    STOP_REASON="session usage limit reached"
    break
  fi

  # ── parse the run ────────────────────────────────────────────────────────────
  if ! jq -e . "$OUTPUT_FILE" >/dev/null 2>&1; then
    echo "  ⚠  Output was not valid JSON (claude exit=$CLAUDE_EXIT). See $OUTPUT_FILE"
    CONSEC_ERRORS=$(( CONSEC_ERRORS + 1 ))
    if [ "$CONSEC_ERRORS" -ge "$MAX_CONSEC_ERRORS" ]; then
      STOP_REASON="$MAX_CONSEC_ERRORS consecutive errors"
      echo "▸ $STOP_REASON — stopping."
      break
    fi
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
  HAS_CHANGES=0; git diff --staged --quiet || HAS_CHANGES=1

  # ── report ─────────────────────────────────────────────────────────────────────
  echo
  ALL_DONE=0
  if [ "$IS_ERROR" = "true" ] || [ "$CLAUDE_EXIT" -ne 0 ]; then
    echo "  Status   : ⚠  ERROR (claude exit=$CLAUDE_EXIT, is_error=$IS_ERROR)"
    CONSEC_ERRORS=$(( CONSEC_ERRORS + 1 ))
  elif grep -q "ALL_TASKS_COMPLETE" "$OUTPUT_FILE" 2>/dev/null; then
    echo "  Status   : 🏁 agent reports all tasks complete"
    ALL_DONE=1; CONSEC_ERRORS=0
  elif grep -q "IMPROVEMENT_COMPLETE" "$OUTPUT_FILE" 2>/dev/null; then
    echo "  Status   : ✓ complete"
    CONSEC_ERRORS=0
  else
    echo "  Status   : ⚠  finished without a completion sentinel"
    CONSEC_ERRORS=0
  fi

  echo "  Cost     : \$$COST  (cumulative \$$TOTAL_COST)"
  echo "  Time     : $(fmt_duration "$DUR_MS")   ·   $TURNS turns"
  echo "  Tokens   : $(human_tokens "$IN_TOK") in / $(human_tokens "$OUT_TOK") out / $(human_tokens "$CACHE_TOK") cached"

  if [ "$HAS_CHANGES" -eq 1 ]; then
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
  if [ "$HAS_CHANGES" -eq 1 ]; then
    SHORT_SUMMARY="$(echo "$RESULT_TEXT" | grep -v '^[[:space:]]*$' | head -n 1 | cut -c1-72)"
    [ -z "$SHORT_SUMMARY" ] && SHORT_SUMMARY="iteration $i"
    git commit -m "auto(iter $i): $SHORT_SUMMARY" --no-verify >/dev/null
    echo "  Committed: $(git rev-parse --short HEAD)"
    CONSEC_NOCHANGE=0
  else
    echo "  Committed: (nothing to commit)"
    CONSEC_NOCHANGE=$(( CONSEC_NOCHANGE + 1 ))
  fi
  echo

  # ── stopping conditions (evaluated AFTER the iteration) ────────────────────────
  if [ "$ALL_DONE" -eq 1 ]; then
    STOP_REASON="all tasks complete"
    echo "▸ Agent signalled ALL_TASKS_COMPLETE. Stopping."
    break
  fi

  if [ "$CONSEC_ERRORS" -ge "$MAX_CONSEC_ERRORS" ]; then
    STOP_REASON="$MAX_CONSEC_ERRORS consecutive errors"
    echo "▸ $STOP_REASON — stopping to avoid burning the session."
    break
  fi

  if [ "$CONSEC_NOCHANGE" -ge "$MAX_CONSEC_NOCHANGE" ]; then
    STOP_REASON="no changes for $MAX_CONSEC_NOCHANGE iterations (treating as done/stuck)"
    echo "▸ $STOP_REASON — stopping."
    break
  fi

  # Budget / time ceilings (skipped when the cap is empty, i.e. disabled).
  ELAPSED_MIN=$(( ($(date +%s) - START_EPOCH) / 60 ))
  if [ -n "$MAX_COST_USD" ] && awk -v c="$TOTAL_COST" -v m="$MAX_COST_USD" 'BEGIN { exit !(c >= m) }'; then
    STOP_REASON="budget cap reached (\$$TOTAL_COST ≥ \$$MAX_COST_USD)"
    echo "▸ $STOP_REASON. Stopping."
    break
  fi
  if [ -n "$MAX_MINUTES" ] && [ "$ELAPSED_MIN" -ge "$MAX_MINUTES" ]; then
    STOP_REASON="time cap reached (${ELAPSED_MIN}min ≥ ${MAX_MINUTES}min)"
    echo "▸ $STOP_REASON. Stopping."
    break
  fi
done

[ -z "$STOP_REASON" ] && STOP_REASON="iteration cap reached ($MAX_ITERATIONS)"

# ── final summary ───────────────────────────────────────────────────────────────
TOTAL_MIN=$(( ($(date +%s) - START_EPOCH) / 60 ))
echo "═════════════════════════════════════════════════════════════════"
echo "  Loop finished — $STOP_REASON"
echo "═════════════════════════════════════════════════════════════════"
echo "  Iterations run : $i"
echo "  Total spend    : \$$TOTAL_COST"
echo "  Total time     : ${TOTAL_MIN} min"
echo "  Commits        : $(git rev-list --count "$HEAD_BEFORE"..HEAD 2>/dev/null || echo '?') new this run on $CURRENT_BRANCH"
echo
echo "  Review changes : git log --oneline ${CURRENT_BRANCH}"
echo "  Per-run logs   : ${RUNS_DIR#$REPO_ROOT/}/"
