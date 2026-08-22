#!/usr/bin/env bash
#
# Container entrypoint.
#
#   root phase : align the kvm group gid with the host's /dev/kvm, fix volume
#                ownership, then drop privileges via gosu. No --privileged.
#   user phase : kvm check -> adb server -> emulator -> wait for boot ->
#                tuning -> uvicorn (foreground, PID-managed, SIGTERM-clean).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

APP_USER="${APP_USER:-androiduser}"
APP_HOME="${APP_HOME:-/home/${APP_USER}}"
AVD_NAME="${AVD_NAME:-pixel6}"
AVD_TEMPLATE_DIR="${AVD_TEMPLATE_DIR:-/opt/avd-template}"
ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-${APP_HOME}/.android/avd}"
export ANDROID_AVD_HOME

BOOT_TIMEOUT="${BOOT_TIMEOUT:-300}"
EMULATOR_RAM="${EMULATOR_RAM:-4096}"
EMULATOR_CORES="${EMULATOR_CORES:-4}"
EMULATOR_EXTRA_ARGS="${EMULATOR_EXTRA_ARGS:-}"
WIPE_DATA="${WIPE_DATA:-false}"
DISABLE_ANIMATIONS="${DISABLE_ANIMATIONS:-true}"
APP_DIR="${APP_DIR:-/opt/app}"
API_HOST="${API_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8080}"
LOG_LEVEL="${LOG_LEVEL:-info}"

log() { printf '[entrypoint] %s\n' "$*"; }
die() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

###############################################################################
# root phase
###############################################################################
if [[ "$(id -u)" -eq 0 ]]; then
    log "running as root: preparing runtime permissions before dropping to '${APP_USER}'."

    if [[ -e /dev/kvm ]]; then
        host_kvm_gid="$(stat -c '%g' /dev/kvm)"
        log "host /dev/kvm gid = ${host_kvm_gid}"

        existing_group="$(getent group "${host_kvm_gid}" | cut -d: -f1 || true)"
        if [[ -n "${existing_group}" ]]; then
            log "gid ${host_kvm_gid} already maps to group '${existing_group}'; adding ${APP_USER} to it."
            usermod -aG "${existing_group}" "${APP_USER}"
        else
            if getent group kvm >/dev/null; then
                log "re-assigning container group 'kvm' to gid ${host_kvm_gid}."
                groupmod -g "${host_kvm_gid}" kvm
            else
                groupadd -g "${host_kvm_gid}" kvm
            fi
            usermod -aG kvm "${APP_USER}"
        fi
    else
        log "WARNING: /dev/kvm not present at root phase; check-kvm.sh will report it properly."
    fi

    # A named volume mounted on the AVD directory arrives root-owned and empty.
    mkdir -p "${ANDROID_AVD_HOME}/${AVD_NAME}.avd"
    chown -R "${APP_USER}" "${APP_HOME}" 2>/dev/null || true

    log "dropping privileges to '${APP_USER}'."
    exec gosu "${APP_USER}" "${BASH_SOURCE[0]}" "$@"
fi

###############################################################################
# unprivileged phase
###############################################################################
log "running as uid=$(id -u) gid=$(id -g) user=$(id -un)."

"${SCRIPT_DIR}/check-kvm.sh"

[[ -n "${API_TOKEN:-}" ]] || die "API_TOKEN is not set. Refusing to start an unauthenticated device bridge. Generate one with: openssl rand -hex 32"

if [[ "${SHELL_MODE:-allowlist}" == "unrestricted" ]]; then
    cat >&2 <<'WARN'

  ############################################################################
  #                                                                          #
  #   WARNING: SHELL_MODE=unrestricted                                       #
  #                                                                          #
  #   /api/shell will execute ARBITRARY commands on the emulator. Anyone      #
  #   holding API_TOKEN has full shell access to the device. Only use this    #
  #   on a trusted, network-isolated deployment.                              #
  #                                                                          #
  ############################################################################

WARN
fi

# --- seed the AVD from the build-time template if the volume is empty --------
avd_dir="${ANDROID_AVD_HOME}/${AVD_NAME}.avd"
avd_ini="${ANDROID_AVD_HOME}/${AVD_NAME}.ini"
template_dir="${AVD_TEMPLATE_DIR}/${AVD_NAME}.avd"

mkdir -p "${ANDROID_AVD_HOME}"

if [[ ! -f "${avd_dir}/config.ini" ]]; then
    [[ -d "${template_dir}" ]] || die "AVD template missing at ${template_dir}; the image is broken."
    log "seeding AVD '${AVD_NAME}' from template (first start or fresh volume)..."
    mkdir -p "${avd_dir}"
    cp -a "${template_dir}/." "${avd_dir}/"
else
    log "reusing existing AVD data in ${avd_dir}."
fi

# The .ini pointer lives outside the volume, so rewrite it every start.
cat > "${avd_ini}" <<INI
avd.ini.encoding=UTF-8
path=${avd_dir}
path.rel=avd/${AVD_NAME}.avd
target=android-${ANDROID_API:-34}
INI

# --- adb ---------------------------------------------------------------------
log "starting adb server..."
adb start-server

EMULATOR_PID=""
UVICORN_PID=""
SHUTTING_DOWN=0

shutdown() {
    [[ "${SHUTTING_DOWN}" -eq 1 ]] && return 0
    SHUTTING_DOWN=1
    log "received termination signal; shutting down gracefully..."

    if [[ -n "${UVICORN_PID}" ]] && kill -0 "${UVICORN_PID}" 2>/dev/null; then
        log "stopping API (pid ${UVICORN_PID})..."
        kill -TERM "${UVICORN_PID}" 2>/dev/null || true
        wait "${UVICORN_PID}" 2>/dev/null || true
    fi

    if [[ -n "${EMULATOR_PID}" ]] && kill -0 "${EMULATOR_PID}" 2>/dev/null; then
        log "asking the emulator to power off (adb emu kill)..."
        adb emu kill >/dev/null 2>&1 || true

        for _ in $(seq 1 30); do
            kill -0 "${EMULATOR_PID}" 2>/dev/null || break
            sleep 1
        done

        if kill -0 "${EMULATOR_PID}" 2>/dev/null; then
            log "emulator still alive; sending SIGTERM."
            kill -TERM "${EMULATOR_PID}" 2>/dev/null || true
            sleep 5
            kill -KILL "${EMULATOR_PID}" 2>/dev/null || true
        fi
    fi

    adb kill-server >/dev/null 2>&1 || true
    log "shutdown complete."
    exit 0
}
# EXIT is trapped too: a failure between launching the emulator and starting
# uvicorn must not leave an orphaned QEMU holding the AVD lock.
trap shutdown SIGTERM SIGINT EXIT

# --- emulator ----------------------------------------------------------------
emu_args=(
    -avd "${AVD_NAME}"
    -no-window
    -no-audio
    -no-boot-anim
    -gpu swiftshader_indirect
    -accel on
    -netdelay none
    -netspeed full
    -no-snapshot-save
    -camera-back none
    -camera-front none
)

if [[ "${WIPE_DATA,,}" == "true" ]]; then
    log "WIPE_DATA=true -> the userdata partition will be reset."
    emu_args+=(-wipe-data)
fi

if [[ -n "${EMULATOR_EXTRA_ARGS}" ]]; then
    # Intentionally word-split: this is an operator-provided argument list.
    # shellcheck disable=SC2206
    extra=(${EMULATOR_EXTRA_ARGS})
    emu_args+=("${extra[@]}")
fi

# -qemu must come last; everything after it is passed straight to QEMU.
emu_args+=(-qemu -m "${EMULATOR_RAM}" -smp "${EMULATOR_CORES}")

log "launching emulator: emulator ${emu_args[*]}"
# Process substitution (not a pipe) so that $! is the emulator itself and not
# the log-prefixing sed - we need the real pid to shut it down cleanly.
emulator "${emu_args[@]}" > >(sed 's/^/[emulator] /') 2>&1 &
EMULATOR_PID=$!

# --- boot --------------------------------------------------------------------
if ! "${SCRIPT_DIR}/wait-for-boot.sh" "${BOOT_TIMEOUT}"; then
    log "emulator failed to boot; tearing down."
    shutdown
    exit 1
fi

# --- post-boot tuning --------------------------------------------------------
if [[ "${DISABLE_ANIMATIONS,,}" == "true" ]]; then
    log "disabling window/transition/animator animations for faster automation..."
    adb shell settings put global window_animation_scale 0 || true
    adb shell settings put global transition_animation_scale 0 || true
    adb shell settings put global animator_duration_scale 0 || true
fi

# Optional props: ADB_PROP_<NAME>=value -> setprop <name> value
# e.g. ADB_PROP_debug_foo=1 sets the property "debug.foo".
while IFS='=' read -r var value; do
    [[ "${var}" == ADB_PROP_* ]] || continue
    prop="${var#ADB_PROP_}"
    prop="${prop//_/.}"
    log "setprop ${prop}=${value}"
    adb shell setprop "${prop}" "${value}" || log "WARNING: setprop ${prop} failed (may need root)."
done < <(env)

if [[ -n "${EXTRA_SETTINGS_CMDS:-}" ]]; then
    log "applying EXTRA_SETTINGS_CMDS..."
    while IFS= read -r line; do
        [[ -z "${line// }" ]] && continue
        log "  adb shell ${line}"
        # shellcheck disable=SC2086
        adb shell ${line} || log "  WARNING: command failed."
    done <<< "${EXTRA_SETTINGS_CMDS//;/$'\n'}"
fi

adb devices -l | sed 's/^/[entrypoint] /'
log "device is ready. Serial: $(adb get-serialno 2>/dev/null || echo unknown)"

# --- API ---------------------------------------------------------------------
log "starting API on ${API_HOST}:${API_PORT} (shell mode: ${SHELL_MODE:-allowlist})"
cd "${APP_DIR}" || die "APP_DIR ${APP_DIR} does not exist"
uvicorn api.main:app \
    --host "${API_HOST}" \
    --port "${API_PORT}" \
    --log-level "${LOG_LEVEL}" \
    --no-access-log \
    --timeout-graceful-shutdown 10 &
UVICORN_PID=$!

wait "${UVICORN_PID}"
exit_code=$?
log "API exited with code ${exit_code}; shutting down the emulator."
shutdown
exit "${exit_code}"
