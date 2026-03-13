#!/usr/bin/env bash
# cacheout-watchdog.sh — Liveness watchdog for the CacheOut headless daemon.
#
# Polls the daemon process at a fixed interval, restarting it if it has
# crashed or become unresponsive. Uses PID + cmdline validation to avoid
# signaling recycled PIDs.
#
# Environment:
#   CACHEOUT_STATE_DIR  State directory (default: ~/.cacheout)
#   CACHEOUT_BIN        Path to CacheOut binary (REQUIRED)
#
# Usage:
#   CACHEOUT_BIN=/path/to/Cacheout ./cacheout-watchdog.sh
#
# The watchdog writes its log to $STATE_DIR/watchdog.log.

set -euo pipefail

STATE_DIR="${CACHEOUT_STATE_DIR:-$HOME/.cacheout}"
BIN="${CACHEOUT_BIN:?CACHEOUT_BIN is required — set to the CacheOut binary path}"

# Reject whitespace in paths: word-splitting ps output for token-boundary
# PID validation is not lossless when argv contains spaces. Until we add
# exact-argv validation (e.g. /proc on Linux), fail fast.
if [[ "$BIN" =~ [[:space:]] ]]; then
    echo "ERROR: CACHEOUT_BIN must not contain whitespace: '$BIN'" >&2
    exit 1
fi
if [[ "$STATE_DIR" =~ [[:space:]] ]]; then
    echo "ERROR: CACHEOUT_STATE_DIR must not contain whitespace: '$STATE_DIR'" >&2
    exit 1
fi

PID_FILE="$STATE_DIR/daemon.pid"
RESTART_MARKER="$STATE_DIR/restart.marker"
SOCK_PATH="$STATE_DIR/status.sock"
LOG_FILE="$STATE_DIR/watchdog.log"
POLL_INTERVAL=5
LIVENESS_TIMEOUT=2  # seconds for socket probe
STARTUP_GRACE=10    # seconds after start before liveness checks

# Cooldown: max 3 restarts in 5 minutes
MAX_RESTARTS=3
COOLDOWN_WINDOW=300  # seconds
RESTART_TIMES=()

log() {
    local ts
    ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    echo "[$ts] $*" >> "$LOG_FILE"
}

# Ensure state directory exists
mkdir -p "$STATE_DIR"

log "Watchdog started. BIN=$BIN STATE_DIR=$STATE_DIR"

validate_pid() {
    # Validate that a PID belongs to our daemon process.
    # Uses token-boundary matching on argv to verify the binary path,
    # --daemon flag, and --state-dir value as discrete tokens.
    # This prevents substring false positives (e.g. /tmp/cacheout matching
    # /tmp/cacheout-staging).
    local pid="$1"

    # Validate PID is a number
    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        return 1
    fi

    # Check process exists
    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    # Get full command line (not just executable name)
    local full_cmd
    full_cmd="$(ps -p "$pid" -o args= 2>/dev/null)" || return 1
    if [[ -z "$full_cmd" ]]; then
        return 1
    fi

    # Token-boundary matching: split into words and check for exact tokens.
    # This avoids substring matches (e.g. BIN=/foo matching args containing /foobar).
    local found_bin=false found_daemon=false found_state_dir=false
    local expect_state_dir_value=false

    # Read tokens from the command line
    read -ra tokens <<< "$full_cmd"
    for token in "${tokens[@]}"; do
        if [[ "$token" == "$BIN" ]]; then
            found_bin=true
        fi
        if [[ "$token" == "--daemon" ]]; then
            found_daemon=true
        fi
        if $expect_state_dir_value; then
            if [[ "$token" == "$STATE_DIR" ]]; then
                found_state_dir=true
            fi
            expect_state_dir_value=false
        fi
        if [[ "$token" == "--state-dir" ]]; then
            expect_state_dir_value=true
        fi
        # Also handle --state-dir=VALUE form
        if [[ "$token" == "--state-dir=$STATE_DIR" ]]; then
            found_state_dir=true
        fi
    done

    $found_bin && $found_daemon && $found_state_dir
}

check_socket_responsive() {
    # Probe the daemon's Unix socket for responsiveness.
    # Sends a health command and expects a JSON response within LIVENESS_TIMEOUT.
    # Returns 0 if responsive, 1 otherwise.
    if [[ ! -S "$SOCK_PATH" ]]; then
        return 1
    fi

    # Use python3 (available on all macOS) for reliable socket probe with timeout.
    # Pass path and timeout via environment to avoid shell interpolation issues
    # (e.g. single quotes in paths breaking embedded Python strings).
    _WD_SOCK_PATH="$SOCK_PATH" _WD_TIMEOUT="$LIVENESS_TIMEOUT" python3 - <<'PY' 2>/dev/null
import os, socket, sys
path = os.environ["_WD_SOCK_PATH"]
timeout = float(os.environ["_WD_TIMEOUT"])
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(timeout)
try:
    s.connect(path)
    s.sendall(b'{"cmd":"health"}\n')
    data = s.recv(65536)
    s.close()
    sys.exit(0 if data else 1)
except:
    sys.exit(1)
PY
}

# check_daemon_state: tri-state liveness check.
# Sets DAEMON_STATE to one of: healthy, missing, unresponsive
# Also sets DAEMON_PID if the daemon's PID was read from the pidfile.
DAEMON_STATE="missing"
DAEMON_PID=""

check_daemon_state() {
    DAEMON_STATE="missing"
    DAEMON_PID=""

    # Check PID file exists
    if [[ ! -f "$PID_FILE" ]]; then
        return
    fi

    DAEMON_PID="$(cat "$PID_FILE" 2>/dev/null)" || { DAEMON_PID=""; return; }

    # Step 1: PID identity validation (is it our daemon?)
    if ! validate_pid "$DAEMON_PID"; then
        DAEMON_PID=""
        return
    fi

    # Step 2: Socket liveness validation (is it responsive?)
    # Skip during startup grace period to avoid restart flapping.
    if [[ -n "${LAST_START_TIME:-}" ]]; then
        local now
        now="$(date +%s)"
        if (( now - LAST_START_TIME < STARTUP_GRACE )); then
            DAEMON_STATE="healthy"
            return
        fi
    fi

    if check_socket_responsive; then
        DAEMON_STATE="healthy"
    else
        DAEMON_STATE="unresponsive"
        log "Daemon PID $DAEMON_PID alive but socket unresponsive"
    fi
}

stop_daemon_pid() {
    # Gracefully stop a daemon by PID. Uses same pattern as restart.marker handler:
    # SIGTERM → wait up to 5s → re-validate → SIGKILL if still ours.
    local pid="$1"
    log "Stopping unresponsive daemon (PID $pid)"
    kill "$pid" 2>/dev/null || true
    # Wait up to 5s for clean exit
    local i
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.5
    done
    # Re-validate before force kill (PID could have been reused)
    if kill -0 "$pid" 2>/dev/null && validate_pid "$pid"; then
        log "Force killing daemon PID $pid"
        kill -9 "$pid" 2>/dev/null || true
    fi
}

check_restart_marker() {
    if [[ -f "$RESTART_MARKER" ]]; then
        log "Restart marker found, daemon requested restart"
        rm -f "$RESTART_MARKER"
        return 0
    fi
    return 1
}

prune_restart_times() {
    local now
    now="$(date +%s)"
    local cutoff=$((now - COOLDOWN_WINDOW))
    local pruned=()
    for t in "${RESTART_TIMES[@]}"; do
        if (( t > cutoff )); then
            pruned+=("$t")
        fi
    done
    RESTART_TIMES=("${pruned[@]}")
}

can_restart() {
    prune_restart_times
    if (( ${#RESTART_TIMES[@]} >= MAX_RESTARTS )); then
        return 1
    fi
    return 0
}

start_daemon() {
    if ! can_restart; then
        log "ERROR: Max restarts ($MAX_RESTARTS) in ${COOLDOWN_WINDOW}s exceeded. Backing off."
        return 1
    fi

    local now
    now="$(date +%s)"
    RESTART_TIMES+=("$now")

    log "Starting daemon: $BIN --daemon --state-dir $STATE_DIR"
    "$BIN" --daemon --state-dir "$STATE_DIR" >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    LAST_START_TIME="$(date +%s)"
    log "Daemon started with PID $new_pid (grace period ${STARTUP_GRACE}s)"
    return 0
}

# Main loop
while true; do
    # Check for restart marker (daemon self-requested restart)
    if check_restart_marker; then
        # Kill old daemon if still running (validate PID identity first)
        if [[ -f "$PID_FILE" ]]; then
            local_pid="$(cat "$PID_FILE" 2>/dev/null)" || true
            if [[ -n "$local_pid" ]] && validate_pid "$local_pid"; then
                stop_daemon_pid "$local_pid"
            else
                log "PID $local_pid from pidfile is stale or not our daemon, skipping kill"
            fi
        fi
        start_daemon || true
        # Skip the health check this iteration — the new daemon needs time
        # to acquire the flock and rewrite the pidfile. Without this, a stale
        # pidfile causes check_daemon_state to report "missing" and double-start.
        sleep "$POLL_INTERVAL"
        continue
    fi

    # Tri-state daemon health check
    check_daemon_state

    case "$DAEMON_STATE" in
        healthy)
            # All good, nothing to do
            ;;
        missing)
            log "Daemon not running, attempting restart"
            start_daemon || true
            ;;
        unresponsive)
            # Daemon process alive but socket unresponsive — stop it first,
            # then start a replacement. Without this, the new daemon would
            # fail to acquire the PID-file flock held by the hung process.
            if [[ -n "$DAEMON_PID" ]]; then
                stop_daemon_pid "$DAEMON_PID"
            fi
            log "Starting replacement daemon after stopping unresponsive instance"
            start_daemon || true
            ;;
    esac

    sleep "$POLL_INTERVAL"
done
