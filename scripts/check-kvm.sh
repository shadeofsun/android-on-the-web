#!/usr/bin/env bash
#
# Verifies that hardware acceleration is actually available.
# The emulator without KVM is ~20x slower and effectively unusable, so we fail
# loudly and instructively instead of silently limping along.
set -euo pipefail

fail() {
    cat >&2 <<'MSG'

================================================================================
  FATAL: /dev/kvm is not usable inside this container.
================================================================================

  The Android emulator REQUIRES hardware virtualisation (KVM). Running without
  it is not supported by this image - it would take 30+ minutes to boot and
  time out.

  Check the HOST:

    1) Is the CPU virtualisation-capable and enabled in BIOS?
         egrep -c '(vmx|svm)' /proc/cpuinfo      # must be > 0

    2) Does the kvm device node exist?
         ls -l /dev/kvm                          # e.g. crw-rw---- root kvm

    3) Is the kvm module loaded?
         lsmod | grep kvm
         sudo modprobe kvm_intel   # or kvm_amd

  Check the CONTAINER:

    4) The device must be passed through. In docker-compose.yml:
         devices:
           - /dev/kvm:/dev/kvm
       or with plain docker:  --device /dev/kvm

  Common cause:

    Most cheap/shared VPS instances (OpenVZ, LXC, and many KVM-on-KVM plans)
    do NOT expose nested virtualisation. You need a BARE-METAL / dedicated
    server, or a cloud instance with nested virtualisation explicitly enabled
    (e.g. GCP nested-virt licence, AWS *.metal, Hetzner dedicated).

================================================================================

MSG
    exit 1
}

if [[ ! -e /dev/kvm ]]; then
    echo "[check-kvm] /dev/kvm does not exist." >&2
    fail
fi

if [[ ! -c /dev/kvm ]]; then
    echo "[check-kvm] /dev/kvm exists but is not a character device." >&2
    fail
fi

if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    echo "[check-kvm] /dev/kvm is not readable/writable by uid=$(id -u) gid=$(id -g) groups=$(id -G)." >&2
    ls -l /dev/kvm >&2 || true
    fail
fi

echo "[check-kvm] OK - /dev/kvm is present and writable (uid=$(id -u), groups=$(id -Gn | tr ' ' ','))."
