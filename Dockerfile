###############################################################################
# Android Emulator (Pixel 6) + REST API + Web UI
#
# Built from the OFFICIAL Google Android SDK command-line tools. No third-party
# emulator images are wrapped.
#
# Layer strategy (cache friendly, largest last):
#   1. apt packages            (~600 MB, changes rarely)
#   2. cmdline-tools           (~150 MB, pinned + checksummed)
#   3. platform-tools+emulator (~400 MB)
#   4. system image            (~2-4 GB, the expensive one -> its own layer)
#   5. AVD template            (small)
#   6. python deps / app code  (tiny, changes often -> last)
###############################################################################

FROM ubuntu:24.04

# --- Versions / URLs. Override with --build-arg, never hardcode elsewhere. ----
# Verify the checksum against https://developer.android.com/studio#command-line-tools-only
# before bumping CMDLINE_TOOLS_VERSION. Set CMDLINE_TOOLS_SHA256="" to skip
# verification (NOT recommended).
ARG CMDLINE_TOOLS_VERSION=11076708
ARG CMDLINE_TOOLS_URL=https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
ARG CMDLINE_TOOLS_SHA256=2d2d50857e4eb553af5a6dc3ad507a17adf43d115264b1afc116f95c92e5e258

ARG ANDROID_API=34
ARG IMAGE_TYPE=google_apis
ARG SYSTEM_IMAGE_ABI=x86_64
ARG AVD_NAME=pixel6
ARG AVD_DEVICE=pixel_6

# Only used as the *initial* gid; entrypoint re-aligns it to the host's /dev/kvm
# gid at runtime, so this default is just a sane starting point.
ARG KVM_GID=104

ARG APP_USER=androiduser
ARG APP_UID=1100
ARG APP_GID=1100

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_HOME=/opt/android-sdk \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

ENV PATH="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${ANDROID_SDK_ROOT}/emulator:${JAVA_HOME}/bin:${PATH}"

# ---------------------------------------------------------------- 1. packages
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        unzip \
        gosu \
        procps \
        socat \
        file \
        openjdk-17-jdk-headless \
        python3 \
        python3-pip \
        python3-venv \
        qemu-kvm \
        libpulse0 \
        libnss3 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxi6 \
        libxtst6 \
        libgl1 \
        libglu1-mesa \
        libx11-6 \
        libxrandr2 \
        libxkbcommon0 \
        libasound2t64 \
    ; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------- 2. user + kvm access
RUN set -eux; \
    groupadd -g "${APP_GID}" "${APP_USER}"; \
    useradd -m -u "${APP_UID}" -g "${APP_GID}" -s /bin/bash "${APP_USER}"; \
    if ! getent group kvm >/dev/null; then groupadd -g "${KVM_GID}" kvm; fi; \
    usermod -aG kvm "${APP_USER}"

# --------------------------------------------------------- 3. cmdline-tools
RUN set -eux; \
    mkdir -p /tmp/cli "${ANDROID_SDK_ROOT}/cmdline-tools"; \
    wget -q -O /tmp/cli/tools.zip "${CMDLINE_TOOLS_URL}"; \
    if [ -n "${CMDLINE_TOOLS_SHA256}" ]; then \
        echo "${CMDLINE_TOOLS_SHA256}  /tmp/cli/tools.zip" | sha256sum -c -; \
    else \
        echo "WARNING: CMDLINE_TOOLS_SHA256 empty - skipping integrity check" >&2; \
    fi; \
    unzip -q /tmp/cli/tools.zip -d /tmp/cli; \
    mv /tmp/cli/cmdline-tools "${ANDROID_SDK_ROOT}/cmdline-tools/latest"; \
    rm -rf /tmp/cli; \
    yes | sdkmanager --licenses >/dev/null

# ------------------------------------------------- 4. platform-tools+emulator
RUN set -eux; \
    sdkmanager --install "platform-tools" "emulator" >/dev/null; \
    rm -rf "${ANDROID_SDK_ROOT}/.temp" /root/.android/cache

# --------------------------------------------------- 5. system image (BIG)
ENV SYSTEM_IMAGE="system-images;android-${ANDROID_API};${IMAGE_TYPE};${SYSTEM_IMAGE_ABI}"
RUN set -eux; \
    sdkmanager --install "${SYSTEM_IMAGE}" >/dev/null; \
    rm -rf "${ANDROID_SDK_ROOT}/.temp" /root/.android/cache

# -------------------------------------------------------- 6. AVD template
# The AVD is created into /opt/avd-template so that a docker volume mounted at
# $HOME/.android/avd/<name>.avd cannot shadow it. entrypoint.sh seeds the volume
# from this template on first boot.
ENV AVD_NAME="${AVD_NAME}" \
    AVD_TEMPLATE_DIR="/opt/avd-template"

RUN set -eux; \
    export ANDROID_AVD_HOME="${AVD_TEMPLATE_DIR}"; \
    mkdir -p "${AVD_TEMPLATE_DIR}"; \
    echo "no" | avdmanager --silent create avd \
        -n "${AVD_NAME}" \
        -k "${SYSTEM_IMAGE}" \
        -d "${AVD_DEVICE}" \
        --abi "${IMAGE_TYPE}/${SYSTEM_IMAGE_ABI}"; \
    CFG="${AVD_TEMPLATE_DIR}/${AVD_NAME}.avd/config.ini"; \
    test -f "${CFG}"; \
    for kv in \
        hw.ramSize=4096 \
        vm.heapSize=576 \
        disk.dataPartition.size=8192 \
        hw.keyboard=yes \
        hw.lcd.width=1080 \
        hw.lcd.height=2400 \
        hw.lcd.density=420 \
        hw.gpu.enabled=yes \
        hw.gpu.mode=swiftshader_indirect \
        hw.audioInput=no \
        hw.audioOutput=no \
        hw.camera.back=none \
        hw.camera.front=none \
        showDeviceFrame=no \
        skin.dynamic=no \
    ; do \
        key="${kv%%=*}"; \
        { grep -v "^${key}=" "${CFG}" || true; } > "${CFG}.tmp"; \
        mv "${CFG}.tmp" "${CFG}"; \
        echo "${kv}" >> "${CFG}"; \
    done; \
    echo "--- patched config.ini ---"; sort "${CFG}"

# ------------------------------------------------------------- 7. python app
COPY api/requirements.txt /opt/app/api/requirements.txt
RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip; \
    /opt/venv/bin/pip install --no-cache-dir -r /opt/app/api/requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

COPY api/ /opt/app/api/
COPY web/ /opt/app/web/
COPY scripts/ /opt/app/scripts/

RUN set -eux; \
    chmod +x /opt/app/scripts/*.sh; \
    chown -R "${APP_UID}:${APP_GID}" /opt/app "${AVD_TEMPLATE_DIR}"; \
    mkdir -p "/home/${APP_USER}/.android/avd"; \
    chown -R "${APP_UID}:${APP_GID}" "/home/${APP_USER}"

ENV APP_USER="${APP_USER}" \
    APP_HOME="/home/${APP_USER}" \
    ANDROID_AVD_HOME="/home/${APP_USER}/.android/avd" \
    WEB_DIR=/opt/app/web \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8080

WORKDIR /opt/app

EXPOSE 8080

# adb clients always talk to 127.0.0.1:5037, so this works regardless of which
# user owns the adb server process.
HEALTHCHECK --interval=20s --timeout=10s --start-period=300s --retries=3 \
    CMD [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]

# Starts as root ONLY to align the kvm group gid with the host device, then
# immediately drops to ${APP_USER} via gosu. --privileged is never required.
ENTRYPOINT ["/opt/app/scripts/entrypoint.sh"]
