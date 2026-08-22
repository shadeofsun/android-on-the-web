#!/usr/bin/env bash
#
# Blocks until the emulator reports sys.boot_completed=1, or times out.
# Usage: wait-for-boot.sh [timeout_seconds]
set -euo pipefail

TIMEOUT="${1:-${BOOT_TIMEOUT:-300}}"
POLL_INTERVAL="${BOOT_POLL_INTERVAL:-2}"
LOG_EVERY="${BOOT_LOG_EVERY:-10}"

log() { printf '[wait-for-boot] %s\n' "$*"; }

log "waiting for a device to appear on adb (timeout ${TIMEOUT}s)..."

start=${SECONDS}

# adb wait-for-device blocks forever, so bound it ourselves.
if ! timeout "${TIMEOUT}" adb wait-for-device; then
    log "ERROR: no device attached to adb after ${TIMEOUT}s."
    adb devices -l || true
    exit 1
fi

log "device attached after $((SECONDS - start))s; polling sys.boot_completed..."

last_log=0
while true; do
    elapsed=$((SECONDS - start))

    if (( elapsed >= TIMEOUT )); then
        log "ERROR: boot did not complete within ${TIMEOUT}s."
        log "last known props:"
        adb shell 'getprop sys.boot_completed; getprop init.svc.bootanim; getprop dev.bootcomplete' 2>&1 | sed 's/^/[wait-for-boot]   /' || true
        exit 1
    fi

    boot_completed="$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n' || true)"
    if [[ "${boot_completed}" == "1" ]]; then
        # boot_completed can flip to 1 slightly before the package manager is
        # ready; wait for pm to answer before declaring victory.
        if adb shell pm path android >/dev/null 2>&1; then
            log "boot completed in ${elapsed}s."
            exit 0
        fi
    fi

    if (( elapsed - last_log >= LOG_EVERY )); then
        last_log=${elapsed}
        bootanim="$(adb shell getprop init.svc.bootanim 2>/dev/null | tr -d '\r\n' || true)"
        log "still booting... ${elapsed}s/${TIMEOUT}s (sys.boot_completed='${boot_completed:-?}', bootanim='${bootanim:-?}')"
    fi

    sleep "${POLL_INTERVAL}"
done
