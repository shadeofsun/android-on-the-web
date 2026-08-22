"""FastAPI application exposing the emulator over HTTP, plus the web UI.

Every route except `/api/health` requires `Authorization: Bearer <API_TOKEN>`.
Blocking adb calls are pushed onto the threadpool so a slow `dumpsys` cannot
stall the event loop (and therefore the screenshot poll driving the UI).
"""

# NOTE: deliberately NO `from __future__ import annotations` here. slowapi wraps
# the endpoint functions, and FastAPI resolves string annotations against the
# wrapper's __globals__ (slowapi's module), which would break every
# `Annotated[..., Depends(...)]` parameter on a rate-limited route.

import asyncio
import json
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from api import adb, mitm, network
from api.auth import StreamTokenDep, TokenDep
from api.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("api")

PROCESS_START = time.monotonic()

limiter = Limiter(
    key_func=get_remote_address,
    enabled=settings.rate_limit_enabled,
    headers_enabled=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "API up | serial=%s shell_mode=%s rate_limit=%s max_apk=%dMB",
        settings.adb_serial,
        settings.shell_mode,
        settings.rate_limit if settings.rate_limit_enabled else "disabled",
        settings.max_apk_mb,
    )
    yield
    logger.info("API shutting down")


app = FastAPI(
    title="Android Emulator API",
    description=(
        "REST control plane for a headless Pixel 6 Android emulator running in "
        "Docker. All endpoints except /api/health require a Bearer token."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #
@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


@app.exception_handler(adb.ShellNotAllowedError)
async def _not_allowed_handler(request: Request, exc: adb.ShellNotAllowedError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


@app.exception_handler(adb.InvalidArgumentError)
async def _invalid_arg_handler(request: Request, exc: adb.InvalidArgumentError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


@app.exception_handler(adb.AdbTimeoutError)
async def _timeout_handler(request: Request, exc: adb.AdbTimeoutError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_504_GATEWAY_TIMEOUT, content={"detail": str(exc)})


@app.exception_handler(adb.AdbUnavailableError)
async def _unavailable_handler(request: Request, exc: adb.AdbUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)}
    )


@app.exception_handler(adb.AdbError)
async def _adb_error_handler(request: Request, exc: adb.AdbError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #
class ShellRequest(BaseModel):
    cmd: str = Field(..., min_length=1, max_length=4096, examples=["getprop ro.product.model"])
    timeout: int | None = Field(default=None, ge=1, le=600)


class ShellResponse(BaseModel):
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class TapRequest(BaseModel):
    x: int = Field(..., ge=0, le=100_000)
    y: int = Field(..., ge=0, le=100_000)


class SwipeRequest(BaseModel):
    x1: int = Field(..., ge=0, le=100_000)
    y1: int = Field(..., ge=0, le=100_000)
    x2: int = Field(..., ge=0, le=100_000)
    y2: int = Field(..., ge=0, le=100_000)
    ms: int = Field(default=300, ge=1, le=60_000)


class TextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class KeyRequest(BaseModel):
    keycode: str = Field(..., min_length=1, max_length=48, examples=["KEYCODE_HOME"])


class SimpleResponse(BaseModel):
    ok: bool = True
    detail: str = ""


class HealthResponse(BaseModel):
    status: str
    boot_completed: bool
    device_state: str
    serial: str
    api_uptime_seconds: int
    shell_mode: str


class InstallResponse(BaseModel):
    ok: bool
    filename: str
    size_bytes: int
    output: str


# --------------------------------------------------------------------------- #
# health (no auth)
# --------------------------------------------------------------------------- #
@app.get("/api/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Liveness + boot state. Deliberately unauthenticated for orchestrators."""
    state = await run_in_threadpool(adb.get_state)
    booted = await run_in_threadpool(adb.is_booted)
    serial = await run_in_threadpool(adb.get_serial) if state == "device" else settings.adb_serial

    return HealthResponse(
        status="ready" if booted else "booting",
        boot_completed=booted,
        device_state=state,
        serial=serial,
        api_uptime_seconds=int(time.monotonic() - PROCESS_START),
        shell_mode=settings.shell_mode,
    )


# --------------------------------------------------------------------------- #
# device
# --------------------------------------------------------------------------- #
@app.get("/api/device", tags=["device"])
@limiter.limit(settings.rate_limit)
async def device(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    """Model, Android version, serial and screen geometry."""
    return await run_in_threadpool(adb.device_info)


@app.post("/api/reboot", response_model=SimpleResponse, tags=["device"])
@limiter.limit("5/minute")
async def reboot(request: Request, response: Response, _: TokenDep) -> SimpleResponse:
    """Reboot the device. Returns immediately; poll /api/health for readiness."""
    await run_in_threadpool(adb.reboot)
    return SimpleResponse(detail="Reboot requested; poll /api/health until boot_completed is true.")


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #
@app.post("/api/shell", response_model=ShellResponse, tags=["shell"])
@limiter.limit(settings.rate_limit)
async def run_shell(
    request: Request, response: Response, body: ShellRequest, _: TokenDep
) -> ShellResponse:
    """Run a command in the device shell, subject to SHELL_MODE policy."""
    result = await run_in_threadpool(adb.shell, body.cmd, timeout=body.timeout)
    return ShellResponse(
        cmd=body.cmd,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
    )


# --------------------------------------------------------------------------- #
# apps
# --------------------------------------------------------------------------- #
@app.get("/api/apps", tags=["apps"])
@limiter.limit(settings.rate_limit)
async def list_apps(
    request: Request,
    response: Response,
    _: TokenDep,
    include_system: Annotated[bool, Query(description="Include system packages")] = False,
) -> dict[str, Any]:
    """List installed packages (third-party only unless include_system=true)."""
    packages = await run_in_threadpool(adb.list_packages, include_system=include_system)
    return {"count": len(packages), "include_system": include_system, "packages": packages}


@app.post("/api/install", response_model=InstallResponse, tags=["apps"])
@limiter.limit("10/minute")
async def install(
    request: Request,
    response: Response,
    _: TokenDep,
    file: Annotated[UploadFile, File(description="APK file to install")],
) -> InstallResponse:
    """Upload and `adb install -r` an APK. Streamed to disk with a size cap."""
    filename = Path(file.filename or "upload.apk").name
    if not filename.lower().endswith(".apk"):
        raise HTTPException(status_code=400, detail="Only .apk files are accepted.")

    tmp_dir = Path(tempfile.mkdtemp(prefix="apk-"))
    tmp_path = tmp_dir / filename
    written = 0

    try:
        with tmp_path.open("wb") as sink:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_apk_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"APK exceeds the {settings.max_apk_mb} MB limit. "
                            f"Raise MAX_APK_MB to allow larger uploads."
                        ),
                    )
                sink.write(chunk)

        if written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        with tmp_path.open("rb") as probe:
            if probe.read(2) != b"PK":
                raise HTTPException(status_code=400, detail="File is not a ZIP/APK archive.")

        result = await run_in_threadpool(adb.install_apk, tmp_path)
        return InstallResponse(
            ok=True,
            filename=filename,
            size_bytes=written,
            output=(result.stdout or result.stderr).strip(),
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.delete("/api/app/{package}", response_model=SimpleResponse, tags=["apps"])
@limiter.limit(settings.rate_limit)
async def uninstall(
    request: Request, response: Response, package: str, _: TokenDep
) -> SimpleResponse:
    """Uninstall a package by name."""
    result = await run_in_threadpool(adb.uninstall_package, package)
    return SimpleResponse(detail=(result.stdout or "Success").strip())


# --------------------------------------------------------------------------- #
# live MJPEG stream
# --------------------------------------------------------------------------- #
def _encode_jpeg(png: bytes, scale: float, quality: int) -> bytes:
    """Decode a screencap PNG and re-encode as a (usually downscaled) JPEG.

    Smaller frames -> more frames per second over the wire. Done in a worker
    thread so it never blocks the event loop.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as img:
        img = img.convert("RGB")
        if 0.0 < scale < 1.0:
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.BILINEAR,
            )
        out = BytesIO()
        img.save(out, format="JPEG", quality=quality)
        return out.getvalue()


@app.get("/api/stream/mjpeg", tags=["device"])
async def stream_mjpeg(
    request: Request,
    _: StreamTokenDep,
    fps: Annotated[int, Query(ge=1, le=60)] = settings.stream_fps,
    scale: Annotated[float, Query(ge=0.1, le=1.0)] = settings.stream_scale,
    quality: Annotated[int, Query(ge=10, le=95)] = settings.stream_quality,
) -> StreamingResponse:
    """Continuous MJPEG (multipart/x-mixed-replace) feed of the device screen.

    Point an <img> straight at it. EventSource-style ?token= is accepted because
    <img> cannot send an Authorization header. The frame rate is capped at
    STREAM_MAX_FPS and is ultimately limited by how fast screencap can run.
    """
    fps = min(fps, settings.stream_max_fps)
    interval = 1.0 / fps
    boundary = "frame"

    async def frames() -> AsyncIterator[bytes]:
        loop = asyncio.get_event_loop()
        while True:
            if await request.is_disconnected():
                break
            started = loop.time()
            try:
                png = await run_in_threadpool(adb.screenshot_png)
                jpeg = await run_in_threadpool(_encode_jpeg, png, scale, quality)
            except adb.AdbError:
                # Device momentarily unavailable (booting, rebooting) - pause and retry.
                await asyncio.sleep(0.5)
                continue
            yield (
                (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode()
                + jpeg
                + b"\r\n"
            )
            elapsed = loop.time() - started
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# screenshot
# --------------------------------------------------------------------------- #
@app.get(
    "/api/screenshot",
    tags=["device"],
    responses={200: {"content": {"image/png": {}}, "description": "Raw PNG"}},
)
@limiter.limit("300/minute")
async def screenshot(request: Request, _: TokenDep) -> Response:
    """Raw PNG of the current framebuffer."""
    png = await run_in_threadpool(adb.screenshot_png)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
@app.post("/api/input/tap", response_model=SimpleResponse, tags=["input"])
@limiter.limit("300/minute")
async def input_tap(
    request: Request, response: Response, body: TapRequest, _: TokenDep
) -> SimpleResponse:
    """Tap at device-space coordinates."""
    await run_in_threadpool(adb.tap, body.x, body.y)
    return SimpleResponse(detail=f"tap {body.x},{body.y}")


@app.post("/api/input/swipe", response_model=SimpleResponse, tags=["input"])
@limiter.limit("300/minute")
async def input_swipe(
    request: Request, response: Response, body: SwipeRequest, _: TokenDep
) -> SimpleResponse:
    """Swipe between two device-space points over `ms` milliseconds."""
    await run_in_threadpool(adb.swipe, body.x1, body.y1, body.x2, body.y2, body.ms)
    return SimpleResponse(detail=f"swipe {body.x1},{body.y1} -> {body.x2},{body.y2} ({body.ms}ms)")


@app.post("/api/input/text", response_model=SimpleResponse, tags=["input"])
@limiter.limit("300/minute")
async def input_text(
    request: Request, response: Response, body: TextRequest, _: TokenDep
) -> SimpleResponse:
    """Type printable ASCII into the focused field."""
    await run_in_threadpool(adb.input_text, body.text)
    return SimpleResponse(detail=f"typed {len(body.text)} characters")


@app.post("/api/input/key", response_model=SimpleResponse, tags=["input"])
@limiter.limit("300/minute")
async def input_key(
    request: Request, response: Response, body: KeyRequest, _: TokenDep
) -> SimpleResponse:
    """Send a keyevent, e.g. KEYCODE_HOME / KEYCODE_BACK / KEYCODE_APP_SWITCH."""
    await run_in_threadpool(adb.press_key, body.keycode)
    return SimpleResponse(detail=f"keyevent {body.keycode.upper()}")


# --------------------------------------------------------------------------- #
# logcat (SSE)
# --------------------------------------------------------------------------- #
def _shell_response(cmd: str, result: adb.AdbResult) -> "ShellResponse":
    return ShellResponse(
        cmd=cmd,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
    )


def _cleanup_task(directory: Path) -> BackgroundTask:
    return BackgroundTask(shutil.rmtree, directory, ignore_errors=True)


def _read_pcap_slice(full: bool) -> bytes:
    """Return the pcap bytes; from the last-clear baseline unless full=True.

    A byte offset inside a pcap is not a valid file start, so we prepend the
    24-byte global header to the sliced records.
    """
    path = network.capture_path()
    raw = path.read_bytes()
    if full or network.state.baseline_offset <= 24:
        return raw
    return raw[:24] + raw[network.state.baseline_offset :]


def _sse(data: str, *, event: str | None = None) -> str:
    lines = "".join(f"data: {line}\n" for line in data.splitlines() or [""])
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}{lines}\n"


@app.get("/api/logcat", tags=["logs"])
async def logcat(
    request: Request,
    _: StreamTokenDep,
    filters: Annotated[
        str | None,
        Query(description="Comma-separated logcat specs, e.g. 'ActivityManager:I,*:E'"),
    ] = None,
    clear: Annotated[bool, Query(description="Clear the buffer before streaming")] = False,
) -> StreamingResponse:
    """Live logcat as Server-Sent Events.

    EventSource cannot set headers, so this endpoint additionally accepts
    `?token=<API_TOKEN>`.
    """
    specs = adb.validate_logcat_filters(filters)

    async def event_stream() -> AsyncIterator[str]:
        yield _sse("connected", event="status")
        emitted = 0
        try:
            async for line in adb.stream_logcat(filters=specs, clear_first=clear):
                if await request.is_disconnected():
                    break
                emitted += 1
                if emitted > settings.logcat_max_lines:
                    yield _sse(
                        f"line limit ({settings.logcat_max_lines}) reached; reconnect to continue",
                        event="status",
                    )
                    break
                yield _sse(line)
        except asyncio.CancelledError:  # client went away
            raise
        except adb.AdbError as exc:
            yield _sse(str(exc), event="error")
        finally:
            yield _sse("closed", event="status")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------- #
# exception handlers for the new modules
# --------------------------------------------------------------------------- #
@app.exception_handler(mitm.MitmError)
async def _mitm_error_handler(request: Request, exc: mitm.MitmError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})


@app.exception_handler(network.CaptureError)
async def _capture_error_handler(request: Request, exc: network.CaptureError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)}
    )


# --------------------------------------------------------------------------- #
# file transfer / root / recording (adb-level)
# --------------------------------------------------------------------------- #
class ForwardRequest(BaseModel):
    local: str = Field(..., examples=["tcp:9000"])
    remote: str = Field(..., examples=["tcp:9000"])


class ReverseRequest(BaseModel):
    remote: str = Field(..., examples=["tcp:9000"])
    local: str = Field(..., examples=["tcp:9000"])


@app.post("/api/push", response_model=SimpleResponse, tags=["files"])
@limiter.limit("30/minute")
async def push(
    request: Request,
    response: Response,
    _: TokenDep,
    file: Annotated[UploadFile, File(description="File to push to the device")],
    dest: Annotated[str, Form(description="Absolute device path, e.g. /sdcard/x")],
) -> SimpleResponse:
    """Upload a file and `adb push` it to an arbitrary device path."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="push-"))
    tmp_path = tmp_dir / (Path(file.filename or "upload.bin").name)
    written = 0
    try:
        with tmp_path.open("wb") as sink:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Upload exceeds the {settings.max_upload_mb} MB limit.",
                    )
                sink.write(chunk)
        await run_in_threadpool(adb.push_file, tmp_path, dest)
        return SimpleResponse(detail=f"pushed {written} bytes to {dest}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/pull", tags=["files"])
@limiter.limit("30/minute")
async def pull(
    request: Request,
    _: StreamTokenDep,
    path: Annotated[str, Query(description="Absolute device path to retrieve")],
) -> FileResponse:
    """`adb pull` a file from the device and stream it back."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="pull-"))
    local = tmp_dir / "pulled.bin"
    await run_in_threadpool(adb.pull_file, path, local)
    size = local.stat().st_size
    if size > settings.max_pull_bytes:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File ({size} bytes) exceeds the {settings.max_pull_mb} MB limit.",
        )
    filename = Path(path).name or "pulled.bin"
    return FileResponse(
        local,
        filename=filename,
        media_type="application/octet-stream",
        background=_cleanup_task(tmp_dir),
    )


@app.post("/api/root", response_model=ShellResponse, tags=["device"])
@limiter.limit("10/minute")
async def root(request: Request, response: Response, _: TokenDep) -> ShellResponse:
    """Restart adbd as root (google_apis image)."""
    result = await run_in_threadpool(adb.adb_root)
    return _shell_response("adb root", result)


@app.post("/api/remount", response_model=ShellResponse, tags=["device"])
@limiter.limit("10/minute")
async def remount(request: Request, response: Response, _: TokenDep) -> ShellResponse:
    """Remount /system read-write (needs root + -writable-system)."""
    result = await run_in_threadpool(adb.adb_remount)
    return _shell_response("adb remount", result)


@app.get("/api/screenrecord", tags=["device"])
@limiter.limit("6/minute")
async def screenrecord(
    request: Request,
    _: StreamTokenDep,
    seconds: Annotated[int, Query(ge=1, le=180)] = 10,
    bitrate_mbps: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> FileResponse:
    """Record the screen for `seconds` and return the mp4."""
    local = await run_in_threadpool(adb.screenrecord, seconds, bit_rate_mbps=bitrate_mbps)
    return FileResponse(
        local,
        filename="screenrecord.mp4",
        media_type="video/mp4",
        background=_cleanup_task(local.parent),
    )


@app.get("/api/forward", tags=["device"])
@limiter.limit(settings.rate_limit)
async def forward_list(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    """List adb port forwards. Note: only useful if the port is also published."""
    return {"forwards": await run_in_threadpool(adb.list_forward)}


@app.post("/api/forward", response_model=SimpleResponse, tags=["device"])
@limiter.limit(settings.rate_limit)
async def forward_add(
    request: Request, response: Response, body: ForwardRequest, _: TokenDep
) -> SimpleResponse:
    await run_in_threadpool(adb.add_forward, body.local, body.remote)
    return SimpleResponse(detail=f"forward {body.local} -> {body.remote}")


@app.delete("/api/forward", response_model=SimpleResponse, tags=["device"])
@limiter.limit(settings.rate_limit)
async def forward_remove(
    request: Request,
    response: Response,
    _: TokenDep,
    local: Annotated[str, Query(description="Local spec to remove, e.g. tcp:9000")],
) -> SimpleResponse:
    await run_in_threadpool(adb.remove_forward, local)
    return SimpleResponse(detail=f"removed forward {local}")


@app.get("/api/reverse", tags=["device"])
@limiter.limit(settings.rate_limit)
async def reverse_list(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    return {"reverses": await run_in_threadpool(adb.list_reverse)}


@app.post("/api/reverse", response_model=SimpleResponse, tags=["device"])
@limiter.limit(settings.rate_limit)
async def reverse_add(
    request: Request, response: Response, body: ReverseRequest, _: TokenDep
) -> SimpleResponse:
    await run_in_threadpool(adb.add_reverse, body.remote, body.local)
    return SimpleResponse(detail=f"reverse {body.remote} -> {body.local}")


@app.delete("/api/reverse", response_model=SimpleResponse, tags=["device"])
@limiter.limit(settings.rate_limit)
async def reverse_remove(
    request: Request,
    response: Response,
    _: TokenDep,
    remote: Annotated[str, Query(description="Remote spec to remove, e.g. tcp:9000")],
) -> SimpleResponse:
    await run_in_threadpool(adb.remove_reverse, remote)
    return SimpleResponse(detail=f"removed reverse {remote}")


# --------------------------------------------------------------------------- #
# network capture - layer 1 (raw packets, every protocol)
# --------------------------------------------------------------------------- #
def _packet_matches(
    packet: network.Packet, proto: str | None, host: str | None, port: int | None
) -> bool:
    if proto and packet.protocol.upper() != proto.upper():
        return False
    if host and host not in (packet.src, packet.dst):
        return False
    if port is not None and port not in (packet.src_port, packet.dst_port):
        return False
    return True


@app.get("/api/network/status", tags=["network"])
@limiter.limit(settings.rate_limit)
async def network_status(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    """Whether capture is on, the pcap size, and how much has been seen."""
    return {
        "capture_enabled": settings.capture_traffic,
        "capture_file": settings.capture_file,
        "file_size_bytes": network.capture_size(),
        "available": network.capture_available(),
        "baseline_offset": network.state.baseline_offset,
    }


@app.get("/api/network/packets", tags=["network"])
@limiter.limit("120/minute")
async def network_packets(
    request: Request,
    response: Response,
    _: TokenDep,
    limit: Annotated[int, Query(ge=1, le=5000)] = 200,
    proto: Annotated[str | None, Query(description="TCP/UDP/DNS/HTTP/TLS/ICMP...")] = None,
    host: Annotated[str | None, Query(description="Match src or dst IP")] = None,
    port: Annotated[int | None, Query(ge=0, le=65535)] = None,
) -> dict[str, Any]:
    """Structured view of recent packets, newest last, with simple filters."""
    if not network.capture_available():
        raise network.CaptureError("no capture file yet (is CAPTURE_TRAFFIC enabled?)")
    cursor = network.PcapCursor(
        offset=network.state.baseline_offset, index=network.state.baseline_index
    )
    packets = await run_in_threadpool(network.read_new_packets, cursor)
    filtered = [p.as_dict() for p in packets if _packet_matches(p, proto, host, port)]
    return {"count": len(filtered), "packets": filtered[-limit:]}


@app.get("/api/network/stats", tags=["network"])
@limiter.limit("60/minute")
async def network_stats(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    """Aggregate totals: packets/bytes per protocol and per host (top talkers)."""
    if not network.capture_available():
        raise network.CaptureError("no capture file yet (is CAPTURE_TRAFFIC enabled?)")
    cursor = network.PcapCursor(
        offset=network.state.baseline_offset, index=network.state.baseline_index
    )
    packets = await run_in_threadpool(network.read_new_packets, cursor)
    by_proto: dict[str, dict[str, int]] = {}
    by_host: dict[str, dict[str, int]] = {}
    total_bytes = 0
    for p in packets:
        total_bytes += p.length
        bp = by_proto.setdefault(p.protocol, {"packets": 0, "bytes": 0})
        bp["packets"] += 1
        bp["bytes"] += p.length
        for endpoint in (p.src, p.dst):
            if not endpoint:
                continue
            bh = by_host.setdefault(endpoint, {"packets": 0, "bytes": 0})
            bh["packets"] += 1
            bh["bytes"] += p.length
    top_hosts = dict(sorted(by_host.items(), key=lambda kv: kv[1]["bytes"], reverse=True)[:20])
    return {
        "total_packets": len(packets),
        "total_bytes": total_bytes,
        "by_protocol": by_proto,
        "top_hosts": top_hosts,
    }


@app.get("/api/network/stream", tags=["network"])
async def network_stream(
    request: Request,
    _: StreamTokenDep,
    proto: Annotated[str | None, Query()] = None,
    host: Annotated[str | None, Query()] = None,
    port: Annotated[int | None, Query(ge=0, le=65535)] = None,
) -> StreamingResponse:
    """Live packet feed as Server-Sent Events (polls the growing pcap)."""
    cursor = network.PcapCursor()
    await run_in_threadpool(network.read_new_packets, cursor)

    async def event_stream() -> AsyncIterator[str]:
        yield _sse("connected", event="status")
        while True:
            if await request.is_disconnected():
                break
            packets = await run_in_threadpool(network.read_new_packets, cursor, limit=500)
            for packet in packets:
                if _packet_matches(packet, proto, host, port):
                    yield _sse(json.dumps(packet.as_dict()))
            await asyncio.sleep(1.0)
        yield _sse("closed", event="status")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/network/pcap", tags=["network"])
@limiter.limit("30/minute")
async def network_pcap(
    request: Request,
    _: StreamTokenDep,
    full: Annotated[bool, Query(description="Ignore the last clear, return everything")] = False,
) -> Response:
    """Download the raw pcap (open in Wireshark). Honours the last clear unless full."""
    path = network.capture_path()
    if not path.is_file():
        raise network.CaptureError("no capture file yet")
    data = await run_in_threadpool(_read_pcap_slice, full)
    return Response(
        content=data,
        media_type="application/vnd.tcpdump.pcap",
        headers={
            "Content-Disposition": 'attachment; filename="capture.pcap"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/network/clear", response_model=SimpleResponse, tags=["network"])
@limiter.limit(settings.rate_limit)
async def network_clear(request: Request, response: Response, _: TokenDep) -> SimpleResponse:
    """Move the baseline to 'now' so subsequent reads start fresh."""
    cursor = network.PcapCursor()
    await run_in_threadpool(network.read_new_packets, cursor)
    network.state.baseline_offset = cursor.offset
    network.state.baseline_index = cursor.index
    return SimpleResponse(detail=f"baseline moved to offset {cursor.offset}")


# --------------------------------------------------------------------------- #
# network capture - layer 2 (mitmproxy, decrypted HTTP, opt-in best-effort)
# --------------------------------------------------------------------------- #
@app.get("/api/network/mitm/status", tags=["network"])
@limiter.limit(settings.rate_limit)
async def mitm_status(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    return await run_in_threadpool(mitm.status)


@app.post("/api/network/mitm/start", tags=["network"])
@limiter.limit("6/minute")
async def mitm_start(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    """Start mitmdump, install its CA into the system store, set the device proxy."""
    return await run_in_threadpool(mitm.start)


@app.post("/api/network/mitm/stop", tags=["network"])
@limiter.limit("6/minute")
async def mitm_stop(request: Request, response: Response, _: TokenDep) -> dict[str, Any]:
    return await run_in_threadpool(mitm.stop)


@app.get("/api/network/mitm/flows", tags=["network"])
@limiter.limit("120/minute")
async def mitm_flows(
    request: Request,
    response: Response,
    _: TokenDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Decrypted HTTP(S) request/response flows captured by mitmproxy."""
    flows = await run_in_threadpool(mitm.read_flows, limit=limit, offset=offset)
    return {"count": len(flows), "flows": flows}


@app.post("/api/network/mitm/clear", response_model=SimpleResponse, tags=["network"])
@limiter.limit(settings.rate_limit)
async def mitm_clear(request: Request, response: Response, _: TokenDep) -> SimpleResponse:
    await run_in_threadpool(mitm.clear_flows)
    return SimpleResponse(detail="mitm flows cleared")


@app.get("/api/network/mitm/ca", tags=["network"])
@limiter.limit(settings.rate_limit)
async def mitm_ca(request: Request, _: StreamTokenDep) -> Response:
    """Download the mitmproxy CA certificate (PEM)."""
    pem = await run_in_threadpool(mitm.ca_pem)
    return Response(
        content=pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="mitmproxy-ca.pem"'},
    )


# --------------------------------------------------------------------------- #
# web UI
# --------------------------------------------------------------------------- #
_web_dir = Path(settings.web_dir)
if _web_dir.is_dir():
    app.mount("/static", StaticFiles(directory=_web_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_web_dir / "index.html", headers={"Cache-Control": "no-store"})
else:  # pragma: no cover - only when running the API standalone
    logger.warning("WEB_DIR %s does not exist; the UI will not be served", _web_dir)
