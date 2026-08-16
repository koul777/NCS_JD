from __future__ import annotations

from pathlib import Path

import pytest

from ncs_jd import desktop_launcher as launcher
from ncs_jd import runtime_resources


SAFE_HEALTH = {
    "status": "ok",
    "tools": {"operator": 0},
    "runtime": {"read_only_mode": True, "operator_tools_enabled": False},
}
READY = {
    "status": "ready",
    "runtime": {
        "read_only_mode": True,
        "operator_tools_enabled": False,
        "database": {"ready": True},
    },
}


def test_runtime_resources_use_source_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NCS_JD_RESOURCE_ROOT", raising=False)
    assert (runtime_resources.resource_root() / "templates" / "index.html").is_file()
    assert runtime_resources.script_path("kordoc_bridge.mjs").is_file()


def test_runtime_resources_use_frozen_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NCS_JD_RESOURCE_ROOT", str(tmp_path))
    assert runtime_resources.resource_root() == tmp_path.resolve()
    assert runtime_resources.template_directory() == tmp_path.resolve() / "templates"
    with pytest.raises(ValueError):
        runtime_resources.script_path("../secret")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {**SAFE_HEALTH, "status": "degraded"},
        {**SAFE_HEALTH, "tools": {"operator": 1}},
        {**SAFE_HEALTH, "runtime": {"read_only_mode": False, "operator_tools_enabled": False}},
        {**SAFE_HEALTH, "runtime": {"read_only_mode": True, "operator_tools_enabled": True}},
    ],
)
def test_mcp_health_rejects_missing_or_unsafe_contract(payload: dict | None) -> None:
    assert launcher._validate_mcp_health(payload) is False


def test_existing_safe_ready_mcp_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([SAFE_HEALTH, READY])
    monkeypatch.setattr(launcher, "_fetch_json", lambda *_args, **_kwargs: next(responses))
    assert launcher.ensure_ncs_mcp(launcher.LauncherConfig()) is None


def test_existing_unsafe_mcp_has_distinct_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = {**SAFE_HEALTH, "tools": {"operator": 1}}
    monkeypatch.setattr(launcher, "_fetch_json", lambda *_args, **_kwargs: unsafe)
    with pytest.raises(launcher.LauncherError) as error:
        launcher.ensure_ncs_mcp(launcher.LauncherConfig())
    assert error.value.exit_code == launcher.EXIT_MCP_UNSAFE


def test_portable_mcp_target_requires_sidecar_and_serving_database(tmp_path: Path) -> None:
    runtime = tmp_path / "NCS_MCP"
    (runtime / "data").mkdir(parents=True)
    (runtime / "NCS_MCP.exe").write_bytes(b"sidecar")
    assert launcher._target_from_root(runtime) is None

    database = runtime / "data" / "ncs_jd_serving.db"
    database.write_bytes(b"sqlite")
    target = launcher._target_from_root(runtime)

    assert target is not None
    assert target.mode == "portable"
    assert target.executable == runtime.resolve() / "NCS_MCP.exe"
    assert target.database == database.resolve()


def test_source_mcp_target_prefers_venv_python(tmp_path: Path) -> None:
    root = tmp_path / "NCS_MCP"
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    (root / "run_ncs_mcp_http.cmd").write_text("@echo off", encoding="utf-8")

    target = launcher._target_from_root(root)

    assert target is not None
    assert target.mode == "source-python"
    assert target.executable == python.resolve()


def test_packaged_target_serves_the_database_shipped_beside_it(tmp_path: Path) -> None:
    database = tmp_path / "data" / "ncs_jd_serving.db"
    target = launcher.NcsMcpLaunchTarget(
        mode="portable",
        root=tmp_path,
        executable=tmp_path / "NCS_MCP.exe",
        database=database,
    )

    assert launcher.resolve_serving_database(target) == database


def test_source_target_prefers_the_built_export_over_the_authoring_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Development must answer from the evidence the package will ship."""

    export = tmp_path / "build" / "portable-data" / "ncs_jd_serving.db"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"sqlite")
    authoring = tmp_path / "NCS_MCP" / "data" / "processed" / "ncs.db"
    authoring.parent.mkdir(parents=True)
    authoring.write_bytes(b"sqlite")
    monkeypatch.setattr(launcher, "_project_serving_database", lambda: export)
    target = launcher.NcsMcpLaunchTarget(
        mode="source-python",
        root=tmp_path / "NCS_MCP",
        executable=tmp_path / "python.exe",
    )

    assert launcher.resolve_serving_database(target) == export


def test_source_target_falls_back_to_the_authoring_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checkout that has never packaged anything still has to run."""

    authoring = tmp_path / "NCS_MCP" / "data" / "processed" / "ncs.db"
    authoring.parent.mkdir(parents=True)
    authoring.write_bytes(b"sqlite")
    monkeypatch.setattr(
        launcher, "_project_serving_database", lambda: tmp_path / "build" / "missing.db"
    )
    target = launcher.NcsMcpLaunchTarget(
        mode="source-python",
        root=tmp_path / "NCS_MCP",
        executable=tmp_path / "python.exe",
    )

    assert launcher.resolve_serving_database(target) == authoring


def test_port_collision_is_reported_before_readiness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NCS_JD_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_port_is_available", lambda *_args: False)
    monkeypatch.setattr(launcher, "_fetch_json", lambda *_args, **_kwargs: None)
    with pytest.raises(launcher.LauncherError) as error:
        launcher.run(launcher.LauncherConfig(open_browser=False), check_only=True)
    assert error.value.exit_code == launcher.EXIT_PORT_IN_USE


def test_check_only_stops_only_mcp_started_by_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = object()
    stopped: list[object] = []
    monkeypatch.setenv("NCS_JD_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(launcher, "_port_is_available", lambda *_args: True)
    monkeypatch.setattr(launcher, "check_kordoc", lambda: Path("node.exe"))
    monkeypatch.setattr(launcher, "ensure_ncs_mcp", lambda _config: process)
    monkeypatch.setattr(launcher, "_stop_process_tree", stopped.append)
    assert launcher.run(launcher.LauncherConfig(open_browser=False), check_only=True) == 0
    assert stopped == [process]


def test_logging_falls_back_when_user_data_directory_is_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked_data_directory = tmp_path / "blocked-data"
    blocked_data_directory.write_text("not a directory", encoding="utf-8")
    fallback_root = tmp_path / "fallback"
    monkeypatch.setenv("NCS_JD_DATA_DIR", str(blocked_data_directory))
    monkeypatch.setattr(launcher.tempfile, "gettempdir", lambda: str(fallback_root))

    log_path = launcher._setup_logging()

    assert log_path == fallback_root.resolve() / "NCS_JD" / "logs" / "launcher.log"
    assert log_path.is_file()
