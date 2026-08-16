"""Local Windows desktop launcher for the NCS JD web application."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import AbstractContextManager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ncs_jd import __version__
from ncs_jd.runtime_resources import (
    log_directory,
    resolve_node_executable,
    resource_root,
    script_path,
    static_directory,
    template_directory,
)


EXIT_OK = 0
EXIT_ALREADY_RUNNING = 10
EXIT_PORT_IN_USE = 20
EXIT_KORDOC_UNAVAILABLE = 30
EXIT_MCP_UNAVAILABLE = 40
EXIT_MCP_UNSAFE = 41
EXIT_SERVER_FAILED = 50
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_MCP_PORT = 8766
MUTEX_NAME = "Local\\NCS_JD_Desktop_Launcher"
LOGGER = logging.getLogger("ncs_jd.launcher")


class LauncherError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class LauncherConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    mcp_port: int = DEFAULT_MCP_PORT
    mcp_root: Path | None = None
    open_browser: bool = True
    startup_timeout_seconds: float = 30.0

    @property
    def app_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def app_health_url(self) -> str:
        return f"{self.app_url}/health"

    @property
    def mcp_health_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/health"

    @property
    def mcp_ready_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/ready"

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"


@dataclass(frozen=True, slots=True)
class NcsMcpLaunchTarget:
    """A source checkout or self-contained sidecar that can serve NCS data."""

    mode: str
    root: Path
    executable: Path
    database: Path | None = None


class WindowsMutex(AbstractContextManager["WindowsMutex"]):
    """Small named-mutex wrapper; a no-op outside Windows for source tests."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self.name = name
        self.handle: int | None = None
        self.already_exists = False

    def __enter__(self) -> "WindowsMutex":
        if os.name != "nt":
            return self
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError("Windows launcher mutex could not be created")
        self.handle = handle
        self.already_exists = kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _fetch_json(url: str, *, timeout_seconds: float = 2.0) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "NCS-JD-Launcher"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                return None
            value = json.loads(response.read(1024 * 1024).decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        return None


def _is_ncs_jd_health(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("service") == "ncs-jd-web")


def _validate_mcp_health(payload: dict[str, Any] | None) -> bool:
    if not payload or payload.get("status") != "ok":
        return False
    runtime = payload.get("runtime")
    tools = payload.get("tools")
    return bool(
        isinstance(runtime, dict)
        and runtime.get("read_only_mode") is True
        and runtime.get("operator_tools_enabled") is False
        and isinstance(tools, dict)
        and tools.get("operator") == 0
    )


def _validate_mcp_ready(payload: dict[str, Any] | None) -> bool:
    if not payload or payload.get("status") != "ready":
        return False
    runtime = payload.get("runtime")
    database = runtime.get("database") if isinstance(runtime, dict) else None
    return bool(
        isinstance(runtime, dict)
        and runtime.get("read_only_mode") is True
        and runtime.get("operator_tools_enabled") is False
        and isinstance(database, dict)
        and database.get("ready") is True
    )


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((host, port))
        except OSError:
            return False
    return True


def _target_from_root(root: Path) -> NcsMcpLaunchTarget | None:
    resolved = root.expanduser().resolve()
    portable_executable = resolved / "NCS_MCP.exe"
    portable_database = resolved / "data" / "ncs_jd_serving.db"
    if portable_executable.is_file() and portable_database.is_file():
        return NcsMcpLaunchTarget(
            mode="portable",
            root=resolved,
            executable=portable_executable,
            database=portable_database,
        )

    source_launcher = resolved / "run_ncs_mcp_http.cmd"
    if source_launcher.is_file():
        source_python = resolved / ".venv" / "Scripts" / "python.exe"
        return NcsMcpLaunchTarget(
            mode="source-python" if source_python.is_file() else "source-cmd",
            root=resolved,
            executable=source_python if source_python.is_file() else source_launcher,
        )
    return None


def _project_serving_database() -> Path | None:
    """Locate the serving export this checkout builds for the package."""

    if getattr(sys, "frozen", False):
        return None
    return Path(__file__).resolve().parents[2] / "build" / "portable-data" / "ncs_jd_serving.db"


def resolve_serving_database(target: NcsMcpLaunchTarget) -> Path | None:
    """Return the database the sidecar should read for this target.

    A packaged target carries its own export.  A source checkout has none beside
    it, so the export this repository builds is preferred over the much larger
    authoring database: development then answers from the same evidence the
    shipped app will, and an export that dropped a unit fails here instead of
    after release.  The authoring database stays as the fallback so a checkout
    that has never packaged anything still runs.
    """

    if target.database is not None:
        return target.database
    candidates = (_project_serving_database(), target.root / "data" / "processed" / "ncs.db")
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def find_ncs_mcp_target(explicit_root: Path | None = None) -> NcsMcpLaunchTarget | None:
    """Public resolution of the sidecar, shared with the agent-loop runner.

    The agent CLI launches its own stdio copy of the same sidecar, so it must
    resolve the executable and serving database exactly the way the launcher
    does rather than keeping a second, drifting copy of that search order.
    """

    return _find_mcp_target(explicit_root)


def _find_mcp_target(explicit_root: Path | None) -> NcsMcpLaunchTarget | None:
    configured = os.environ.get("NCS_MCP_ROOT")
    executable_dir = Path(sys.executable).resolve().parent
    candidates = [
        explicit_root,
        Path(configured).expanduser() if configured else None,
        executable_dir / "NCS_MCP",
        executable_dir.parent / "NCS_MCP",
        Path(r"C:\workspace\NCS_MCP"),
    ]
    for candidate in candidates:
        if candidate is not None:
            target = _target_from_root(candidate)
            if target is not None:
                return target
    return None


def _find_mcp_root(explicit_root: Path | None) -> Path | None:
    """Backward-compatible root lookup used by diagnostics and older callers."""

    target = _find_mcp_target(explicit_root)
    return target.root if target is not None else None


def _start_mcp(target: NcsMcpLaunchTarget, config: LauncherConfig) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "NCS_MCP_HOST": "127.0.0.1",
            "NCS_MCP_PORT": str(config.mcp_port),
            "NCS_MCP_READ_ONLY": "1",
            "NCS_MCP_ENABLE_OPERATOR_TOOLS": "0",
        }
    )
    database = resolve_serving_database(target)
    if database is not None:
        environment["NCS_DB_PATH"] = str(database)
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    if target.mode == "source-python":
        source_path = target.root / "src"
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{source_path}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(source_path)
        )
        command = [
            str(target.executable),
            "-m",
            "ncs_mcp.server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(config.mcp_port),
        ]
    elif target.mode == "portable":
        command = [
            str(target.executable),
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(config.mcp_port),
        ]
    else:
        command = [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            f'"{target.executable}"',
        ]

    preferred_destination = log_directory()
    fallback_destination = Path(tempfile.gettempdir()).resolve() / "NCS_JD" / "logs"
    mcp_log = None
    mcp_log_path = preferred_destination / "ncs_mcp.log"
    for destination in dict.fromkeys((preferred_destination, fallback_destination)):
        candidate = destination / "ncs_mcp.log"
        try:
            destination.mkdir(parents=True, exist_ok=True)
            mcp_log = candidate.open("ab")
        except OSError:
            continue
        mcp_log_path = candidate
        break
    if mcp_log is None:
        raise OSError("NCS MCP log file could not be opened")
    LOGGER.info("Starting NCS MCP target mode=%s root=%s log=%s", target.mode, target.root, mcp_log_path)
    try:
        return subprocess.Popen(
            command,
            cwd=target.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=mcp_log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    finally:
        mcp_log.close()


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    LOGGER.info("Stopping NCS MCP process tree started by this launcher")
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            process.terminate()
            process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        LOGGER.warning("NCS MCP process tree did not stop cleanly")


def ensure_ncs_mcp(config: LauncherConfig) -> subprocess.Popen[bytes] | None:
    health = _fetch_json(config.mcp_health_url)
    if health is not None:
        if not _validate_mcp_health(health):
            raise LauncherError(
                "8766 포트의 NCS MCP가 읽기 전용/operator 0 모드가 아닙니다.",
                exit_code=EXIT_MCP_UNSAFE,
            )
        if not _validate_mcp_ready(_fetch_json(config.mcp_ready_url)):
            raise LauncherError("NCS MCP 데이터베이스가 준비되지 않았습니다.", exit_code=EXIT_MCP_UNAVAILABLE)
        return None

    target = _find_mcp_target(config.mcp_root)
    if target is None:
        raise LauncherError(
            "공식 NCS MCP 실행기를 찾지 못했습니다. NCS_MCP_ROOT를 설정하세요.",
            exit_code=EXIT_MCP_UNAVAILABLE,
        )
    process = _start_mcp(target, config)
    deadline = time.monotonic() + config.startup_timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        health = _fetch_json(config.mcp_health_url)
        if health is not None and not _validate_mcp_health(health):
            _stop_process_tree(process)
            raise LauncherError(
                "시작된 NCS MCP의 읽기 전용 안전 계약 검사가 실패했습니다.",
                exit_code=EXIT_MCP_UNSAFE,
            )
        if _validate_mcp_health(health) and _validate_mcp_ready(_fetch_json(config.mcp_ready_url)):
            return process
        time.sleep(0.25)
    _stop_process_tree(process)
    raise LauncherError(
        f"NCS MCP가 {config.startup_timeout_seconds:g}초 안에 준비되지 않았습니다.",
        exit_code=EXIT_MCP_UNAVAILABLE,
    )


def check_kordoc() -> Path:
    node = resolve_node_executable()
    bridges = (script_path("kordoc_bridge.mjs"), script_path("kordoc_hwpx_bridge.mjs"))
    if node is None:
        raise LauncherError("Node.js 실행 파일을 찾지 못했습니다.", exit_code=EXIT_KORDOC_UNAVAILABLE)
    if any(not bridge.is_file() for bridge in bridges):
        raise LauncherError("Kordoc bridge 리소스가 누락되었습니다.", exit_code=EXIT_KORDOC_UNAVAILABLE)
    script = (
        'const k=await import("kordoc");'
        'const r=["blocksToMarkdown","detectFormat","detectZipFormat","extractFormSchema",'
        '"fillForm","markdownToHwpx","parse","parseHwpx","renderHwpxToSvg","validateHwpx"];'
        'if(k.VERSION!=="4.2.9")process.exit(2);'
        'if(r.some(n=>typeof k[n]!=="function"))process.exit(3);'
    )
    try:
        result = subprocess.run(
            [str(node), "--input-type=module", "--eval", script],
            cwd=resource_root(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LauncherError("Node/Kordoc self-check를 실행할 수 없습니다.", exit_code=EXIT_KORDOC_UNAVAILABLE) from exc
    if result.returncode != 0:
        raise LauncherError(
            f"Kordoc 4.2.9 readiness 검사가 실패했습니다 (code={result.returncode}).",
            exit_code=EXIT_KORDOC_UNAVAILABLE,
        )
    return node


def _build_app(config: LauncherConfig, node: Path):
    from ncs_jd.adapters.kordoc_hwpx_renderer import KordocHwpxRenderer
    from ncs_jd.adapters.kordoc_parser import KordocDocumentParser
    from ncs_jd.web.app import KordocReadiness, create_connected_app

    parser = KordocDocumentParser(
        bridge_path=script_path("kordoc_bridge.mjs"),
        node_executable=str(node),
    )
    renderer = KordocHwpxRenderer(
        bridge_path=script_path("kordoc_hwpx_bridge.mjs"),
        node_executable=str(node),
    )
    return create_connected_app(
        renderer=renderer,
        parser=parser,
        template_directory=template_directory(),
        static_directory=static_directory(),
        kordoc_readiness=KordocReadiness(True),
    )


def _open_browser_when_ready(config: LauncherConfig) -> None:
    deadline = time.monotonic() + config.startup_timeout_seconds
    while time.monotonic() < deadline:
        if _is_ncs_jd_health(_fetch_json(config.app_health_url, timeout_seconds=0.5)):
            webbrowser.open(config.app_url, new=2)
            return
        time.sleep(0.2)
    LOGGER.error("Browser was not opened because the local server never became healthy")


def _setup_logging() -> Path:
    preferred_destination = log_directory()
    fallback_destination = Path(tempfile.gettempdir()).resolve() / "NCS_JD" / "logs"
    handler: RotatingFileHandler | None = None
    failures: list[tuple[Path, OSError]] = []
    log_path = preferred_destination / "launcher.log"
    for destination in dict.fromkeys((preferred_destination, fallback_destination)):
        candidate = destination / "launcher.log"
        try:
            destination.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                candidate,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        except OSError as exc:
            failures.append((candidate, exc))
            continue
        log_path = candidate
        break
    if handler is None:
        details = "; ".join(f"{path}: {error}" for path, error in failures)
        raise OSError(f"런처 로그 파일을 만들 수 없습니다: {details}")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    old_handlers = LOGGER.handlers[:]
    LOGGER.handlers.clear()
    for old_handler in old_handlers:
        old_handler.close()
    LOGGER.addHandler(handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        LOGGER.addHandler(console)
    LOGGER.setLevel(logging.INFO)
    if log_path != preferred_destination / "launcher.log":
        LOGGER.warning(
            "기본 로그 경로를 사용할 수 없어 임시 경로를 사용합니다: %s",
            log_path,
        )
    return log_path


def diagnostics(config: LauncherConfig) -> dict[str, Any]:
    node = resolve_node_executable()
    target = _find_mcp_target(config.mcp_root)
    return {
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "resource_root": str(resource_root()),
        "node": str(node) if node else None,
        "kordoc_bridges": {
            "parser": script_path("kordoc_bridge.mjs").is_file(),
            "renderer": script_path("kordoc_hwpx_bridge.mjs").is_file(),
        },
        "generation_mode": "deterministic",
        "external_ai_required": False,
        "mcp_root": str(target.root if target else ""),
        "mcp_runtime": {
            "mode": target.mode if target else None,
            "executable": str(target.executable) if target else None,
            "database": str(target.database) if target and target.database else None,
        },
        "log_directory": str(log_directory()),
    }


def run(config: LauncherConfig, *, check_only: bool = False) -> int:
    log_path = _setup_logging()
    LOGGER.info("NCS JD %s starting; logs=%s", __version__, log_path)
    if config.host != DEFAULT_HOST:
        raise LauncherError("데스크톱 앱은 127.0.0.1에만 바인딩할 수 있습니다.", exit_code=EXIT_SERVER_FAILED)
    if not _port_is_available(config.host, config.port):
        existing = _fetch_json(config.app_health_url)
        if _is_ncs_jd_health(existing):
            if config.open_browser:
                webbrowser.open(config.app_url, new=2)
            return EXIT_ALREADY_RUNNING
        raise LauncherError(
            f"포트 {config.port}를 다른 프로그램이 사용 중입니다.",
            exit_code=EXIT_PORT_IN_USE,
        )

    node = check_kordoc()
    os.environ.update(
        {
            "NCS_MCP_URL": config.mcp_url,
            "NCS_MCP_HEALTH_URL": config.mcp_health_url,
            "NCS_MCP_READY_URL": config.mcp_ready_url,
        }
    )
    mcp_process = ensure_ncs_mcp(config)
    try:
        if check_only:
            LOGGER.info("All desktop runtime checks passed")
            return EXIT_OK
        app = _build_app(config, node)
        if config.open_browser:
            threading.Thread(target=_open_browser_when_ready, args=(config,), daemon=True).start()
        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(app, host=config.host, port=config.port, log_level="info", access_log=False)
        )
        LOGGER.info("NCS JD is available at %s; press Ctrl+C to stop", config.app_url)
        server.run()
        return EXIT_OK if server.started else EXIT_SERVER_FAILED
    finally:
        if mcp_process is not None:
            _stop_process_tree(mcp_process)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NCS JD local Windows launcher")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mcp-port", type=int, default=DEFAULT_MCP_PORT)
    parser.add_argument("--ncs-mcp-root", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--version", action="version", version=f"NCS JD {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not 1 <= arguments.port <= 65535 or not 1 <= arguments.mcp_port <= 65535:
        _parser().error("ports must be between 1 and 65535")
    config = LauncherConfig(
        port=arguments.port,
        mcp_port=arguments.mcp_port,
        mcp_root=arguments.ncs_mcp_root,
        open_browser=not arguments.no_browser,
    )
    if arguments.diagnostics:
        print(json.dumps(diagnostics(config), ensure_ascii=False, indent=2))
        return EXIT_OK
    try:
        with WindowsMutex() as mutex:
            if mutex.already_exists:
                if _is_ncs_jd_health(_fetch_json(config.app_health_url)):
                    if config.open_browser:
                        webbrowser.open(config.app_url, new=2)
                    return EXIT_ALREADY_RUNNING
                raise LauncherError("NCS JD 런처가 이미 실행 중입니다.", exit_code=EXIT_ALREADY_RUNNING)
            return run(config, check_only=arguments.check_only)
    except LauncherError as exc:
        LOGGER.error("%s (exit=%s)", exc, exc.exit_code)
        return exc.exit_code
    except KeyboardInterrupt:
        return EXIT_OK
    except Exception:
        LOGGER.exception("Unexpected launcher failure")
        return EXIT_SERVER_FAILED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_ALREADY_RUNNING",
    "EXIT_KORDOC_UNAVAILABLE",
    "EXIT_MCP_UNAVAILABLE",
    "EXIT_MCP_UNSAFE",
    "EXIT_OK",
    "EXIT_PORT_IN_USE",
    "EXIT_SERVER_FAILED",
    "LauncherConfig",
    "LauncherError",
    "diagnostics",
    "ensure_ncs_mcp",
    "main",
    "run",
]
