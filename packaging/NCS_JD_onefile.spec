# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file specification for the convenient root launcher."""

import os
import shutil
from pathlib import Path

project_root = Path(SPECPATH).parent.resolve()
node_executable = os.environ.get("NCS_JD_PACKAGE_NODE_EXE") or shutil.which("node")
if not node_executable or not Path(node_executable).is_file():
    raise SystemExit("Set NCS_JD_PACKAGE_NODE_EXE to a usable node.exe")

node_modules = Path(
    os.environ.get("NCS_JD_PACKAGE_NODE_MODULES")
    or project_root / "node_modules"
).resolve()
if not (node_modules / "kordoc" / "package.json").is_file():
    raise SystemExit("Run npm ci before packaging; Kordoc 4.2.9 is missing")

license_file = project_root / "build" / "windows-runtime" / "LICENSE.node.txt"
license_datas = [(str(license_file), "licenses")] if license_file.is_file() else []
datas = [
    (str(project_root / "templates"), "templates"),
    (str(project_root / "static"), "static"),
    (str(project_root / "scripts" / "kordoc_bridge.mjs"), "scripts"),
    (str(project_root / "scripts" / "kordoc_hwpx_bridge.mjs"), "scripts"),
    (str(project_root / "package.json"), "."),
    (str(project_root / "package-lock.json"), "."),
    (str(node_modules), "node_modules"),
] + license_datas

a = Analysis(
    [str(project_root / "src" / "ncs_jd" / "desktop_launcher.py")],
    pathex=[str(project_root / "src")],
    binaries=[(str(Path(node_executable).resolve()), "runtime")],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PIL",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "black",
        "docutils",
        "jedi",
        "matplotlib",
        "nbformat",
        "notebook",
        "numpy",
        "pandas",
        "pygame",
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
    a.binaries,
    a.datas,
    [],
    name="NCS_JD",
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
