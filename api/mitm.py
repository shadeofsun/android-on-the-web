"""HTTPS interception (layer 2), opt-in and best-effort.

Runs `mitmdump` as a regular proxy inside the container, points the device's
global HTTP proxy at it (10.0.2.2 is the host loopback as seen from the guest),
and installs mitmproxy's CA into the Android system trust store so that apps
which do NOT pin certificates will present decrypted traffic.

Honest caveats:
  * Needs the emulator launched with -writable-system (MITM_ENABLED=true wires
    that up in entrypoint.sh) plus `adb root` + `adb remount`.
  * Apps that pin certificates (many banking / anti-fraud SDKs) will still fail
    to connect or will bypass the proxy - that is by design on their part.
  * This device-side path has not been exercised against a live emulator here;
    treat the first run as a smoke test.

All process management and file reading below is plain stdlib and is testable.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from api import adb
from api.config import settings

_ADDON = "/opt/app/mitm/addon.py"
_PIDFILE = "/tmp/mitmdump.pid"  # noqa: S108 - container-local, single tenant


class MitmError(RuntimeError):
    """Something went wrong starting or querying the interceptor."""


def _pid() -> int | None:
    try:
        pid = int(Path(_PIDFILE).read_text())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def is_running() -> bool:
    return _pid() is not None


def status() -> dict[str, object]:
    proxy = ""
    try:
        proxy = adb.getprop("net.global.http_proxy") or ""
        if not proxy:
            result = adb.shell_raw(["settings", "get", "global", "http_proxy"], timeout=10)
            proxy = result.stdout.strip()
    except adb.AdbError:
        proxy = "unknown"

    ca = Path(settings.mitm_ca_file)
    flows = Path(settings.mitm_flows_file)
    return {
        "enabled_at_boot": settings.mitm_enabled,
        "running": is_running(),
        "pid": _pid(),
        "port": settings.mitm_port,
        "device_proxy": proxy,
        "ca_present": ca.is_file(),
        "flows_captured": _count_lines(flows),
    }


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open("rb") as fh:
        return sum(1 for _ in fh)


def _ensure_ca() -> Path:
    """Start mitmdump once (throwaway) if needed so it writes its CA files."""
    ca = Path(settings.mitm_ca_file)
    if ca.is_file():
        return ca
    Path(settings.mitm_dir).mkdir(parents=True, exist_ok=True)
    # Running mitmdump briefly generates the CA in confdir, then we stop it.
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [
            settings.mitm_binary,
            "--set",
            f"confdir={settings.mitm_dir}",
            "--listen-port",
            "0",
            "-q",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if ca.is_file():
                break
            import time

            time.sleep(0.2)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not ca.is_file():
        raise MitmError("mitmproxy did not generate a CA certificate")
    return ca


def _install_ca_into_system_store(ca: Path) -> str:
    """Push the CA into /system/etc/security/cacerts under its subject hash."""
    result = subprocess.run(  # noqa: S603 - fixed argv, trusted input file
        ["openssl", "x509", "-inform", "PEM", "-subject_hash_old", "-in", str(ca)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    subject_hash = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if not subject_hash:
        raise MitmError(f"could not compute cert hash: {result.stderr.strip()}")

    adb.adb_root()
    adb.adb_remount()
    adb.shell_raw(["mount", "-o", "rw,remount", "/"], timeout=15)
    adb.shell_raw(["mount", "-o", "rw,remount", "/system"], timeout=15)

    remote = f"/system/etc/security/cacerts/{subject_hash}.0"
    adb.push_file(ca, remote)
    adb.shell_raw(["chmod", "644", remote], timeout=15)
    return remote


def start() -> dict[str, object]:
    if is_running():
        return status()

    Path(settings.mitm_dir).mkdir(parents=True, exist_ok=True)
    ca = _ensure_ca()
    installed_at = _install_ca_into_system_store(ca)

    env = dict(os.environ)
    env["MITM_FLOWS_FILE"] = settings.mitm_flows_file

    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [
            settings.mitm_binary,
            "--mode",
            "regular",
            "--listen-host",
            "0.0.0.0",  # noqa: S104 - reached only from the guest via 10.0.2.2
            "--listen-port",
            str(settings.mitm_port),
            "--set",
            f"confdir={settings.mitm_dir}",
            "--set",
            "block_global=false",
            "-s",
            _ADDON,
            "-q",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(_PIDFILE).write_text(str(proc.pid))

    # Point the device at the proxy (10.0.2.2 = host loopback from the guest).
    adb.shell_raw(
        ["settings", "put", "global", "http_proxy", f"10.0.2.2:{settings.mitm_port}"],
        timeout=15,
    )
    result = status()
    result["ca_installed_at"] = installed_at
    return result


def stop() -> dict[str, object]:
    pid = _pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    Path(_PIDFILE).unlink(missing_ok=True)
    try:
        adb.shell_raw(["settings", "put", "global", "http_proxy", ":0"], timeout=15)
    except adb.AdbError:
        pass
    return status()


def read_flows(*, limit: int, offset: int = 0) -> list[dict[str, object]]:
    path = Path(settings.mitm_flows_file)
    if not path.is_file():
        return []
    flows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i < offset:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                flows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(flows) >= limit:
                break
    return flows


def clear_flows() -> None:
    Path(settings.mitm_flows_file).write_text("")


def ca_pem() -> str:
    ca = Path(settings.mitm_ca_file)
    if not ca.is_file():
        raise MitmError("CA not generated yet; start interception first")
    return ca.read_text()
