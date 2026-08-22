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

    allowed_binaries_set: frozenset[str] = field(init=False, default=frozenset())

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_binaries_set", frozenset(self.shell_allowed_binaries))

    @property
    def max_apk_bytes(self) -> int:
        return self.max_apk_mb * 1024 * 1024

    @property
    def unrestricted_shell(self) -> bool:
        return self.shell_mode == SHELL_MODE_UNRESTRICTED


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
    )


settings: Settings = load_settings()
