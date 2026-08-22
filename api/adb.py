"""Every adb invocation in this project goes through here.

Rules enforced by this module:

* `subprocess` is always called with an **argument list**; `shell=True` is never
  used anywhere in the codebase.
* every call carries a timeout;
* the target serial is always pinned with `-s`;
* failures become typed exceptions instead of raw CalledProcessError.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from api.config import settings

logger = logging.getLogger("api.adb")

# Characters that let a device-side shell chain or substitute commands. In
# allowlist mode a command containing any of these is rejected outright, before
# tokenisation, so no clever quoting can smuggle one through.
_SHELL_METACHARACTERS: tuple[str, ...] = (
    ";",
    "&",
    "|",
    "`",
    "$(",
    "${",
    ">",
    "<",
    "\n",
    "\r",
    "\\",
)

# `input text` on Android takes a single token; spaces must be sent as %s.
_INPUT_TEXT_ALLOWED = re.compile(r"^[\x20-\x7e]*$")

_KEYCODE_RE = re.compile(r"^(KEYCODE_[A-Z0-9_]{1,40}|[0-9]{1,4})$")
_LOGCAT_FILTER_RE = re.compile(r"^[A-Za-z0-9_.*:\-]{1,64}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


class AdbError(RuntimeError):
    """Base class for anything that goes wrong talking to the device."""


class AdbTimeoutError(AdbError):
    """An adb invocation exceeded its timeout."""


class AdbUnavailableError(AdbError):
    """The adb binary is missing or the device is not attached."""


class ShellNotAllowedError(AdbError):
    """A shell command was rejected by the allowlist policy."""


class InvalidArgumentError(AdbError):
    """Caller supplied a value that failed validation."""


@dataclass(frozen=True, slots=True)
class AdbResult:
    """Outcome of a single adb invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def raise_for_status(self, what: str) -> AdbResult:
        if not self.ok:
            detail = (self.stderr or self.stdout).strip() or f"exit code {self.exit_code}"
            raise AdbError(f"{what} failed: {detail}")
        return self


def _base_argv() -> list[str]:
    return [settings.adb_binary, "-s", settings.adb_serial]


def run(
    args: list[str],
    *,
    timeout: int | None = None,
    check: bool = False,
    what: str = "adb command",
) -> AdbResult:
    """Run `adb -s <serial> <args...>` and capture text output."""
    argv = _base_argv() + args
    effective_timeout = timeout if timeout is not None else settings.adb_timeout
    started = time.monotonic()

    logger.debug("adb exec: %s", argv)
    try:
        completed = subprocess.run(  # noqa: S603 - list argv, no shell
            argv,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbUnavailableError(f"adb binary {settings.adb_binary!r} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbTimeoutError(f"{what} timed out after {effective_timeout}s") from exc

    result = AdbResult(
        exit_code=completed.returncode,
        stdout=completed.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n"),
        stderr=completed.stderr.decode("utf-8", errors="replace").replace("\r\n", "\n"),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if check:
        result.raise_for_status(what)
    return result


def run_binary(args: list[str], *, timeout: int | None = None, what: str = "adb command") -> bytes:
    """Run an adb command whose stdout is binary (e.g. `exec-out screencap -p`)."""
    argv = _base_argv() + args
    effective_timeout = timeout if timeout is not None else settings.adb_timeout

    try:
        completed = subprocess.run(  # noqa: S603 - list argv, no shell
            argv,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbUnavailableError(f"adb binary {settings.adb_binary!r} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbTimeoutError(f"{what} timed out after {effective_timeout}s") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AdbError(f"{what} failed: {detail or completed.returncode}")
    return completed.stdout


# --------------------------------------------------------------------------- #
# shell policy
# --------------------------------------------------------------------------- #
def validate_shell_command(command: str) -> list[str]:
    """Turn a user-supplied shell string into a safe device-side argv.

    In allowlist mode the command must tokenise cleanly, contain no shell
    metacharacters, and start with an approved binary. Tokens are re-quoted with
    `shlex.quote` before being handed to adb, so the device-side shell treats
    them as literals.
    """
    stripped = command.strip()
    if not stripped:
        raise InvalidArgumentError("cmd must not be empty")
    if len(stripped) > 4096:
        raise InvalidArgumentError("cmd must be at most 4096 characters")

    if settings.unrestricted_shell:
        logger.warning("unrestricted shell command: %s", stripped)
        return [stripped]

    for meta in _SHELL_METACHARACTERS:
        if meta in stripped:
            raise ShellNotAllowedError(
                f"Character sequence {meta!r} is not permitted in "
                f"SHELL_MODE=allowlist. Command chaining, pipes, redirection and "
                f"substitution are disabled."
            )

    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        raise InvalidArgumentError(f"Could not parse command: {exc}") from exc

    if not tokens:
        raise InvalidArgumentError("cmd must not be empty")

    binary = tokens[0]
    if binary not in settings.allowed_binaries_set:
        allowed = ", ".join(sorted(settings.allowed_binaries_set))
        raise ShellNotAllowedError(
            f"Binary {binary!r} is not allowed. Permitted binaries: {allowed}. "
            f"Set SHELL_MODE=unrestricted to lift this restriction (not recommended)."
        )

    return [" ".join(shlex.quote(token) for token in tokens)]


def shell(command: str, *, timeout: int | None = None) -> AdbResult:
    """Run a validated command inside the device shell."""
    device_argv = validate_shell_command(command)
    return run(
        ["shell", *device_argv],
        timeout=timeout if timeout is not None else settings.shell_timeout,
        what="shell command",
    )


def shell_raw(argv: list[str], *, timeout: int | None = None) -> AdbResult:
    """Internal helper for commands this module builds itself (never user text).

    Each token is quoted for the device-side shell.
    """
    quoted = " ".join(shlex.quote(token) for token in argv)
    return run(["shell", quoted], timeout=timeout, what=f"shell {argv[0]}")


# --------------------------------------------------------------------------- #
# device state
# --------------------------------------------------------------------------- #
def getprop(name: str) -> str:
    result = shell_raw(["getprop", name], timeout=10)
    return result.stdout.strip()


def is_booted() -> bool:
    try:
        return getprop("sys.boot_completed") == "1"
    except AdbError:
        return False


def get_serial() -> str:
    result = run(["get-serialno"], timeout=10)
    return result.stdout.strip() if result.ok else settings.adb_serial


def get_state() -> str:
    result = run(["get-state"], timeout=10)
    return result.stdout.strip() if result.ok else "offline"


def screen_size() -> tuple[int, int] | None:
    """Return (width, height) in pixels, preferring the override size."""
    result = shell_raw(["wm", "size"], timeout=10)
    if not result.ok:
        return None
    override: tuple[int, int] | None = None
    physical: tuple[int, int] | None = None
    for line in result.stdout.splitlines():
        match = re.search(r"(\d+)x(\d+)", line)
        if not match:
            continue
        dims = (int(match.group(1)), int(match.group(2)))
        if "Override" in line:
            override = dims
        elif "Physical" in line:
            physical = dims
    return override or physical


def screen_density() -> int | None:
    result = shell_raw(["wm", "density"], timeout=10)
    if not result.ok:
        return None
    override: int | None = None
    physical: int | None = None
    for line in result.stdout.splitlines():
        match = re.search(r"(\d+)\s*$", line.strip())
        if not match:
            continue
        if "Override" in line:
            override = int(match.group(1))
        elif "Physical" in line:
            physical = int(match.group(1))
    return override or physical


def device_info() -> dict[str, object]:
    """Model, Android version, serial and resolution in a single round trip set."""
    size = screen_size()
    return {
        "serial": get_serial(),
        "state": get_state(),
        "model": getprop("ro.product.model"),
        "manufacturer": getprop("ro.product.manufacturer"),
        "device": getprop("ro.product.device"),
        "android_version": getprop("ro.build.version.release"),
        "sdk_int": _safe_int(getprop("ro.build.version.sdk")),
        "build_id": getprop("ro.build.id"),
        "abi": getprop("ro.product.cpu.abi"),
        "screen_width": size[0] if size else None,
        "screen_height": size[1] if size else None,
        "density": screen_density(),
        "boot_completed": is_booted(),
    }


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# screenshot
# --------------------------------------------------------------------------- #
def screenshot_png() -> bytes:
    """Capture the framebuffer as PNG bytes."""
    data = run_binary(
        ["exec-out", "screencap", "-p"],
        timeout=settings.screenshot_timeout,
        what="screenshot",
    )
    if not data.startswith(b"\x89PNG"):
        raise AdbError("screencap did not return a PNG (is the device booted?)")
    return data


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
def tap(x: int, y: int) -> AdbResult:
    _require_non_negative(x=x, y=y)
    return shell_raw(["input", "tap", str(x), str(y)], timeout=15).raise_for_status("tap")


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> AdbResult:
    _require_non_negative(x1=x1, y1=y1, x2=x2, y2=y2)
    if not 1 <= duration_ms <= 60_000:
        raise InvalidArgumentError("ms must be between 1 and 60000")
    return shell_raw(
        ["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
        timeout=max(15, duration_ms // 1000 + 10),
    ).raise_for_status("swipe")


def input_text(text: str) -> AdbResult:
    """Type text on the device.

    `input text` treats its argument as a single token and interprets `%s` as a
    space, so we escape accordingly and then shell-quote the whole thing.
    """
    if not text:
        raise InvalidArgumentError("text must not be empty")
    if len(text) > 4096:
        raise InvalidArgumentError("text must be at most 4096 characters")
    if not _INPUT_TEXT_ALLOWED.match(text):
        raise InvalidArgumentError(
            "text may only contain printable ASCII; use /api/input/key for "
            "special keys and IME-specific input for other scripts."
        )

    encoded = text.replace("%", "%%").replace(" ", "%s")
    return shell_raw(["input", "text", encoded], timeout=30).raise_for_status("input text")


def press_key(keycode: str) -> AdbResult:
    normalised = keycode.strip().upper()
    if not _KEYCODE_RE.match(normalised):
        raise InvalidArgumentError("keycode must look like 'KEYCODE_HOME' or a numeric keycode")
    return shell_raw(["input", "keyevent", normalised], timeout=15).raise_for_status("keyevent")


def _require_non_negative(**coords: int) -> None:
    for name, value in coords.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidArgumentError(f"{name} must be an integer")
        if not 0 <= value <= 100_000:
            raise InvalidArgumentError(f"{name} must be between 0 and 100000")


# --------------------------------------------------------------------------- #
# packages
# --------------------------------------------------------------------------- #
def validate_package(package: str) -> str:
    candidate = package.strip()
    if not _PACKAGE_RE.match(candidate) or len(candidate) > 255:
        raise InvalidArgumentError(f"{package!r} is not a valid Android package name")
    return candidate


def list_packages(*, include_system: bool = False) -> list[str]:
    argv = ["pm", "list", "packages"]
    if not include_system:
        argv.append("-3")
    result = shell_raw(argv, timeout=60).raise_for_status("pm list packages")
    return sorted(
        line.removeprefix("package:").strip()
        for line in result.stdout.splitlines()
        if line.startswith("package:")
    )


def install_apk(apk_path: Path) -> AdbResult:
    if not apk_path.is_file():
        raise InvalidArgumentError(f"APK not found at {apk_path}")
    result = run(
        ["install", "-r", "-t", "-g", str(apk_path)],
        timeout=settings.install_timeout,
        what="install",
    )
    # `adb install` can exit 0 while printing "Failure [...]".
    combined = f"{result.stdout}\n{result.stderr}"
    if "Failure" in combined or "Error" in combined:
        raise AdbError(f"install failed: {combined.strip()}")
    result.raise_for_status("install")
    return result


def uninstall_package(package: str) -> AdbResult:
    validated = validate_package(package)
    result = run(["uninstall", validated], timeout=120, what="uninstall")
    combined = f"{result.stdout}\n{result.stderr}"
    if "Failure" in combined:
        raise AdbError(f"uninstall failed: {combined.strip()}")
    result.raise_for_status("uninstall")
    return result


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def reboot() -> None:
    run(["reboot"], timeout=30, what="reboot")


def wait_for_boot(timeout: int | None = None) -> bool:
    deadline = time.monotonic() + (timeout if timeout is not None else settings.boot_timeout)
    while time.monotonic() < deadline:
        if is_booted():
            return True
        time.sleep(2)
    return False


# --------------------------------------------------------------------------- #
# logcat streaming
# --------------------------------------------------------------------------- #
def validate_logcat_filters(filters: str | None) -> list[str]:
    """Validate `TAG:LEVEL` filter specs; anything odd is rejected, not escaped."""
    if not filters:
        return []
    specs: list[str] = []
    for raw in filters.split(","):
        spec = raw.strip()
        if not spec:
            continue
        if not _LOGCAT_FILTER_RE.match(spec):
            raise InvalidArgumentError(
                f"Invalid logcat filter {spec!r}; expected e.g. 'ActivityManager:I' or '*:E'"
            )
        specs.append(spec)
    return specs


async def stream_logcat(*, filters: list[str], clear_first: bool = False) -> AsyncIterator[str]:
    """Yield logcat lines as they arrive. Cancelling the iterator kills adb."""
    if clear_first:
        run(["logcat", "-c"], timeout=15, what="logcat -c")

    argv = [*_base_argv(), "logcat", "-v", "threadtime"]
    if filters:
        argv += filters

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        assert process.stdout is not None
        while True:
            raw_line = await process.stdout.readline()
            if not raw_line:
                break
            yield raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
    finally:
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()


# --------------------------------------------------------------------------- #
# file transfer, root, recording, port forwarding (adb-level, not shell)
# --------------------------------------------------------------------------- #
# These call adb subcommands directly with an argv list, so device-side paths
# are passed as literal arguments - there is no shell to inject into, and no
# path validation is imposed beyond "not empty". "Everything allowed" applies.
def push_file(local_path: Path, device_path: str) -> AdbResult:
    if not local_path.is_file():
        raise InvalidArgumentError(f"local file not found: {local_path}")
    if not device_path.strip():
        raise InvalidArgumentError("device_path must not be empty")
    return run(
        ["push", str(local_path), device_path],
        timeout=settings.transfer_timeout,
        what="push",
        check=True,
    )


def pull_file(device_path: str, local_path: Path) -> AdbResult:
    if not device_path.strip():
        raise InvalidArgumentError("device_path must not be empty")
    result = run(
        ["pull", device_path, str(local_path)],
        timeout=settings.transfer_timeout,
        what="pull",
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if not local_path.exists() or "error:" in combined.lower():
        raise AdbError(f"pull failed: {combined.strip() or 'file not retrieved'}")
    return result


def adb_root() -> AdbResult:
    """Restart adbd as root. Works on -eng / google_apis images."""
    result = run(["root"], timeout=30, what="adb root")
    # adb root prints to stdout even on the no-op path; surface it as-is.
    if "cannot run as root" in (result.stdout + result.stderr).lower():
        raise AdbError(result.stdout.strip() or result.stderr.strip())
    run(["wait-for-device"], timeout=60, what="wait-for-device")
    return result


def adb_unroot() -> AdbResult:
    result = run(["unroot"], timeout=30, what="adb unroot")
    run(["wait-for-device"], timeout=60, what="wait-for-device")
    return result


def adb_remount() -> AdbResult:
    """Make /system and friends writable (needs root + -writable-system)."""
    return run(["remount"], timeout=60, what="adb remount")


def screenrecord(seconds: int, *, bit_rate_mbps: int | None = None) -> Path:
    """Record the screen on the device, then pull the mp4 to a temp file.

    Returns the local path; the caller is responsible for cleaning it up.
    """
    if not 1 <= seconds <= settings.screenrecord_max_seconds:
        raise InvalidArgumentError(
            f"seconds must be between 1 and {settings.screenrecord_max_seconds}"
        )
    import tempfile
    import time as _time

    remote = f"/sdcard/screenrecord-{int(_time.monotonic() * 1000)}.mp4"
    argv = ["screenrecord", "--time-limit", str(seconds)]
    if bit_rate_mbps is not None:
        if not 1 <= bit_rate_mbps <= 200:
            raise InvalidArgumentError("bit_rate_mbps must be between 1 and 200")
        argv += ["--bit-rate", str(bit_rate_mbps * 1_000_000)]
    argv.append(remote)

    # screenrecord blocks for the whole duration; give it headroom.
    shell_raw(argv, timeout=seconds + 30).raise_for_status("screenrecord")
    # Flush to disk before pulling.
    _time.sleep(1)

    local = Path(tempfile.mkdtemp(prefix="rec-")) / "screenrecord.mp4"
    try:
        pull_file(remote, local)
    finally:
        run(["shell", "rm", "-f", remote], timeout=15, what="rm recording")
    return local


def list_forward() -> list[dict[str, str]]:
    result = run(["forward", "--list"], timeout=15, what="forward --list")
    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            entries.append({"serial": parts[0], "local": parts[1], "remote": parts[2]})
    return entries


def add_forward(local: str, remote: str) -> AdbResult:
    _require_forward_spec(local=local, remote=remote)
    return run(["forward", local, remote], timeout=15, what="forward", check=True)


def remove_forward(local: str) -> AdbResult:
    _require_forward_spec(local=local)
    return run(["forward", "--remove", local], timeout=15, what="forward --remove", check=True)


def list_reverse() -> list[dict[str, str]]:
    result = run(["reverse", "--list"], timeout=15, what="reverse --list")
    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            entries.append({"serial": parts[0], "remote": parts[1], "local": parts[2]})
        elif len(parts) == 2:
            entries.append({"remote": parts[0], "local": parts[1]})
    return entries


def add_reverse(remote: str, local: str) -> AdbResult:
    _require_forward_spec(local=local, remote=remote)
    return run(["reverse", remote, local], timeout=15, what="reverse", check=True)


def remove_reverse(remote: str) -> AdbResult:
    _require_forward_spec(remote=remote)
    return run(["reverse", "--remove", remote], timeout=15, what="reverse --remove", check=True)


_FORWARD_SPEC_RE = re.compile(
    r"^(tcp:\d{1,5}|localabstract:[\w.\-]+|localreserved:[\w.\-]+|jdwp:\d+)$"
)


def _require_forward_spec(**specs: str) -> None:
    for name, value in specs.items():
        if not _FORWARD_SPEC_RE.match(value.strip()):
            raise InvalidArgumentError(
                f"{name} must be like 'tcp:8080' or 'localabstract:name', got {value!r}"
            )
