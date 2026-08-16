# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir specification for the read-only NCS MCP sidecar."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).parent.resolve()
ncs_mcp_root = Path(os.environ["NCS_MCP_SOURCE_ROOT"]).resolve()
ncs_mcp_source = ncs_mcp_root / "src"
if not (ncs_mcp_source / "ncs_mcp" / "server.py").is_file():
    raise SystemExit(f"NCS MCP source was not found: {ncs_mcp_source}")

hiddenimports = sorted(
    set(
        collect_submodules("mcp.server")
        + collect_submodules("mcp.shared")
        + collect_submodules("uvicorn")
        + collect_submodules("sse_starlette")
    )
)
datas = collect_data_files("mcp")

a = Analysis(
    [str(project_root / "packaging" / "ncs_mcp_sidecar_launcher.py")],
    pathex=[str(ncs_mcp_source)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "black",
        "matplotlib",
        "notebook",
        "numpy",
        "pandas",
        "pytest",
        "scipy",
        "sphinx",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NCS_MCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NCS_MCP",
)
