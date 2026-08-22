"""Runtime configuration, read once from the environment.

Every knob is an environment variable with an explicit default so the container
is reproducible and the README table stays authoritative.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Final

DEFAULT_ALLOWED_BINARIES: Final[tuple[str, ...]] = (
    "pm",
    "am",
    "input",
    "dumpsys",
    "getprop",
    "setprop",
    "settings",
    "screencap",
    "wm",
    "ls",
    "cat",
    "cmd",
    "monkey",
    "logcat",
    "ps",
    "df",
)

SHELL_MODE_ALLOWLIST: Final[str] = "allowlist"
SHELL_MODE_UNRESTRICTED: Final[str] = "unrestricted"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - config error path
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - config error path
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of the process configuration."""

    api_token: str
    host: str = "0.0.0.0"  # noqa: S104 - container-internal; Traefik fronts it
    port: int = 8080

    adb_binary: str = "adb"
    adb_serial: str = "emulator-5554"
    adb_timeout: int = 30
    boot_timeout: int = 300

    shell_mode: str = SHELL_MODE_ALLOWLIST
    shell_allowed_binaries: tuple[str, ...] = DEFAULT_ALLOWED_BINARIES
    shell_timeout: int = 30

    max_apk_mb: int = 200
    install_timeout: int = 300

    rate_limit: str = "60/minute"
    rate_limit_enabled: bool = True

    logcat_max_lines: int = 5000
    web_dir: str = "/opt/app/web"
    screenshot_timeout: int = 30

    # live MJPEG stream
    stream_fps: int = 8
    stream_max_fps: int = 30
    stream_scale: float = 0.5
    stream_quality: int = 60

    # file transfer / recording
    max_upload_mb: int = 2048
    max_pull_mb: int = 2048
    transfer_timeout: int = 600
    screenrecord_max_seconds: int = 180

    # network capture (layer 1: raw packets, every protocol)
    capture_traffic: bool = True
    capture_dir: str = "/home/androiduser/captures"
    capture_file: str = "/home/androiduser/captures/traffic.pcap"

    # HTTPS interception (layer 2: decrypted HTTP, opt-in, best-effort)
    mitm_enabled: bool = False
    mitm_port: int = 8081
    mitm_dir: str = "/home/androiduser/mitm"
    mitm_binary: str = "mitmdump"

    allowed_binaries_set: frozenset[str] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_binaries_set", frozenset(self.shell_allowed_binaries))

    @property
    def max_apk_bytes(self) -> int:
        return self.max_apk_mb * 1024 * 1024

    @property
    def unrestricted_shell(self) -> bool:
        return self.shell_mode == SHELL_MODE_UNRESTRICTED

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_pull_bytes(self) -> int:
        return self.max_pull_mb * 1024 * 1024

    @property
    def mitm_flows_file(self) -> str:
        return f"{self.mitm_dir}/flows.jsonl"

    @property
    def mitm_ca_file(self) -> str:
        return f"{self.mitm_dir}/mitmproxy-ca-cert.pem"


def load_settings() -> Settings:
    """Build Settings from the environment, failing fast on misconfiguration."""
    token = os.environ.get("API_TOKEN", "")
    if not token.strip():
        raise SystemExit(
            "FATAL: API_TOKEN is not set.\n"
            "This service exposes shell access to an Android device; it will not "
            "start without authentication.\n"
            "Generate one with:  openssl rand -hex 32"
        )
    if len(token) < 16:
        raise SystemExit(
            f"FATAL: API_TOKEN is too short ({len(token)} chars). "
            "Use at least 16 characters; 32+ recommended."
        )

    shell_mode = _env_str("SHELL_MODE", SHELL_MODE_ALLOWLIST).strip().lower()
    if shell_mode not in {SHELL_MODE_ALLOWLIST, SHELL_MODE_UNRESTRICTED}:
        raise SystemExit(
            f"FATAL: SHELL_MODE must be '{SHELL_MODE_ALLOWLIST}' or "
            f"'{SHELL_MODE_UNRESTRICTED}', got {shell_mode!r}."
        )

    if shell_mode == SHELL_MODE_UNRESTRICTED:
        print(
            "\n"
            "  ##########################################################################\n"
            "  #                                                                        #\n"
            "  #   WARNING: SHELL_MODE=unrestricted                                     #\n"
            "  #                                                                        #\n"
            "  #   /api/shell will run ARBITRARY commands on the emulator. Anyone with  #\n"
            "  #   API_TOKEN effectively has a root-adjacent shell on the device.       #\n"
            "  #   Only enable this on a trusted, network-isolated deployment.          #\n"
            "  #                                                                        #\n"
            "  ##########################################################################\n",
            file=sys.stderr,
            flush=True,
        )

    return Settings(
        api_token=token,
        host=_env_str("API_HOST", "0.0.0.0"),  # noqa: S104
        port=_env_int("API_PORT", 8080),
        adb_binary=_env_str("ADB_BINARY", "adb"),
        adb_serial=_env_str("ADB_SERIAL", "emulator-5554"),
        adb_timeout=_env_int("ADB_TIMEOUT", 30),
        boot_timeout=_env_int("BOOT_TIMEOUT", 300),
        shell_mode=shell_mode,
        shell_allowed_binaries=_env_csv("SHELL_ALLOWED_BINARIES", DEFAULT_ALLOWED_BINARIES),
        shell_timeout=_env_int("SHELL_TIMEOUT", 30),
        max_apk_mb=_env_int("MAX_APK_MB", 200),
        install_timeout=_env_int("INSTALL_TIMEOUT", 300),
        rate_limit=_env_str("RATE_LIMIT", "60/minute"),
        rate_limit_enabled=_env_bool("RATE_LIMIT_ENABLED", True),
        logcat_max_lines=_env_int("LOGCAT_MAX_LINES", 5000),
        web_dir=_env_str("WEB_DIR", "/opt/app/web"),
        screenshot_timeout=_env_int("SCREENSHOT_TIMEOUT", 30),
        stream_fps=_env_int("STREAM_FPS", 8),
        stream_max_fps=_env_int("STREAM_MAX_FPS", 30),
        stream_quality=_env_int("STREAM_QUALITY", 60),
        stream_scale=_env_float("STREAM_SCALE", 0.5),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 2048),
        max_pull_mb=_env_int("MAX_PULL_MB", 2048),
        transfer_timeout=_env_int("TRANSFER_TIMEOUT", 600),
        screenrecord_max_seconds=_env_int("SCREENRECORD_MAX_SECONDS", 180),
        capture_traffic=_env_bool("CAPTURE_TRAFFIC", True),
        capture_dir=_env_str("CAPTURE_DIR", "/home/androiduser/captures"),
        capture_file=_env_str("CAPTURE_FILE", "/home/androiduser/captures/traffic.pcap"),
        mitm_enabled=_env_bool("MITM_ENABLED", False),
        mitm_port=_env_int("MITM_PORT", 8081),
        mitm_dir=_env_str("MITM_DIR", "/home/androiduser/mitm"),
        mitm_binary=_env_str("MITM_BINARY", "mitmdump"),
    )


settings: Settings = load_settings()
