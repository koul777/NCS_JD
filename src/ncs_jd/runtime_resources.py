"""Resolve application resources in source and PyInstaller onedir builds."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resource_root() -> Path:
    """Return the immutable application resource directory."""

    override = os.environ.get("NCS_JD_RESOURCE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[2]


def template_directory() -> Path:
    return resource_root() / "templates"


def static_directory() -> Path:
    return resource_root() / "static"


def script_path(name: str) -> Path:
    if Path(name).name != name:
        raise ValueError("resource script name must not contain a path")
    return resource_root() / "scripts" / name


def resolve_node_executable() -> Path | None:
    """Prefer the bundled Node runtime and otherwise use the system runtime."""

    override = os.environ.get("NCS_JD_NODE_EXE")
    candidates = [
        Path(override).expanduser() if override else None,
        resource_root() / "runtime" / "node.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    system_node = shutil.which("node")
    return Path(system_node).resolve() if system_node else None


def user_data_directory() -> Path:
    configured = os.environ.get("NCS_JD_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base).resolve() / "NCS_JD"


def log_directory() -> Path:
    return user_data_directory() / "logs"


__all__ = [
    "log_directory",
    "resolve_node_executable",
    "resource_root",
    "script_path",
    "static_directory",
    "template_directory",
    "user_data_directory",
]
