#!/bin/sh
# One scheduled cycle. Run by launchd; see `signal schedule`.
#
# Two rules. It never exits non-zero, because a failing agent gets throttled and
# then forgotten by launchd, and a silent stop is the worst outcome here: the
# ground truth this project grades itself on passes and does not come back. And
# every failure is made noisy, in the log and on screen, rather than swallowed.

PROJECT=$(cd "$(dirname "$0")/.." && pwd)
LOG_DIR="$PROJECT/data/logs"
LOG="$LOG_DIR/scheduled.log"
MAX_LOG_BYTES=5242880

mkdir -p "$LOG_DIR" || exit 0
cd "$PROJECT" || exit 0

# Keep one previous log rather than growing without limit.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$MAX_LOG_BYTES" ]; then
    mv "$LOG" "$LOG.1"
fi

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

run() {
    if ! "$PROJECT/.venv/bin/python" -m sigarb.cli "$@" >> "$LOG" 2>&1; then
        echo "$(stamp) FAILED: signal $*" >> "$LOG"
        osascript -e "display notification \"signal $* failed. See data/logs/scheduled.log\" with title \"Signal Arbitrage\"" >/dev/null 2>&1
    fi
}

echo "$(stamp) cycle start" >> "$LOG"
run update

# Everything below runs at most once per market day, tracked by a marker file.
# The cycle itself fires every few minutes, so an hour-range test alone would run
# the peak call sixty times a morning: the record would survive that, because
# calls insert-or-ignore, but sixty notification attempts would not be survivable
# by the person reading them.
#
# Market time, not local time. IESO stamps EST year round and the call is keyed
# on the market day, so gating on Toronto local time would put an hour of the
# year's cycles on the wrong side of the boundary.
EST_HOUR=$(TZ=EST date +%H)
EST_DAY=$(TZ=EST date +%Y-%m-%d)

once_today() {
    marker="$LOG_DIR/last-$1"
    [ "$(cat "$marker" 2>/dev/null)" = "$EST_DAY" ] && return 1
    printf '%s' "$EST_DAY" > "$marker"
    return 0
}

# The peak call is a morning decision, made before the afternoon it is about.
if [ "$EST_HOUR" -ge 6 ] && [ "$EST_HOUR" -le 10 ] && once_today peak-call; then
    run peak --days 3 --notify
fi

# Put the raw payloads somewhere that is not this disk. They are the only copy of
# anything older than the upstream archive window.
if [ "$EST_HOUR" -ge 4 ] && once_today backup; then
    run backup
fi

echo "$(stamp) cycle end" >> "$LOG"
exit 0
