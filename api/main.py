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
from starlette.concurrency import run_in_threadpool

from api import adb
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
