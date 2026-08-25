name: Trial EMA(26/70/240) Strategy — Position Monitor (temporary experiment)

# TEMPORARY — separate from Daily Scan / Paper Trading. Does not call
# scripts/run_paper_trading.py or scripts/generate_full_report.py, so
# the live production pipeline's own schedule is completely unaffected
# by this workflow existing or running.
#
# This is the INTRADAY MONITOR half only (--mode monitor) — runs 4x/day
# during market hours and checks ONLY already-open positions (opened by
# the previous day's scan, see trial_ema_bb_strategy.yml) against a
# CURRENT price:
#   - stop-loss hit          -> exit (full close)
#   - neither hit             -> hold, unchanged
#   - target1 (3%) hit        -> stop-loss shifts to target1 (locks in
#                                 the gain), position keeps running
# NEVER opens a new position — that is trial_ema_bb_strategy.yml's job
# alone.
#
# Schedule: 9:30, 11:30, 13:30, 14:30 IST (converted to UTC, IST = UTC+5:30):
#   9:30 IST  -> 04:00 UTC
#   11:30 IST -> 06:00 UTC
#   13:30 IST -> 08:00 UTC
#   14:30 IST -> 09:00 UTC

on:
  schedule:
    - cron: "0 4 * * *"  # 9:30 AM IST
    - cron: "0 6 * * *"  # 11:30 AM IST
    - cron: "0 8 * * *"  # 1:30 PM IST
    - cron: "0 9 * * *"  # 2:30 PM IST
  workflow_dispatch: {}

# BUG FIX (2026-08-25): same fix as trial_ema_bb_strategy.yml — this used
# to share "repo-write-lock" with the whole production pipeline. GitHub
# Actions keeps only ONE pending run per concurrency group, so a new
# trigger queued behind a busy group CANCELS whatever was already
# waiting. This monitor fires 4x/day including right at/after market
# open (9:30 IST) — close enough to morning_executor (9:16 IST) and
# paper_trading (right after it) to collide and get one of them silently
# cancelled. Own, separate group restores the isolation this workflow's
# header comment always claimed.
concurrency:
  group: trial-strategy-write-lock
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  trial_monitor:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run trial position monitor
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          PYTHONPATH=. python scripts/trial_ema_bb_strategy.py --mode monitor

      - name: Commit updated trial state back to repo
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add -A -- storage/trial_trades/
          git diff --staged --quiet || git commit -m "Trial EMA strategy position monitor: $(date -u +'%Y-%m-%d %H:%M UTC')"
          PUSH_OK=false
          for i in 1 2 3 4 5; do
            if git push; then
              PUSH_OK=true
              break
            fi
            echo "Push rejected (likely a concurrent workflow pushed first) — pulling and retrying ($i/5)..."
            git stash --include-untracked
            git pull --rebase origin main
            git stash pop || true
            sleep 60
          done
          if [ "$PUSH_OK" = false ]; then
            echo "::error::Push failed after 5 attempts — commit exists locally but was NOT pushed to remote."
            exit 1
          fi
