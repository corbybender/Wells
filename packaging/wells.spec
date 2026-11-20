# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone `wells` executable.

Build (from the repo root, with the project installed in the active env):

    pyinstaller packaging/wells.spec --noconfirm

Output: dist/wells/  (onedir — see the rationale below).

onedir, not onefile: the harness shells out to itself, loads Textual/Rich and
LangGraph lazily, and ships skill packages as *files on disk*. onefile would
re-extract ~50 MB into a temp dir on every single invocation — including the
`wells` child processes that bench/fleet/evolve spawn, which would make those
loops dramatically slower. onedir starts instantly and keeps the embedded
data files addressable.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

# Collect the whole package: submodules (many tools are imported lazily by
# name), plus data files (AGENT.md, builtin_skills/**/SKILL.md).
datas, binaries, hiddenimports = collect_all("wells")

# Never ship local run artifacts that happen to sit inside the source tree.
_ARTIFACT_DIRS = ("/.wells/", "/.wells_index/", "/.pytest_cache/", "/__pycache__/")
datas = [d for d in datas if not any(part in d[0].replace("\\", "/") for part in _ARTIFACT_DIRS)]
binaries = [b for b in binaries if not any(part in b[0].replace("\\", "/") for part in _ARTIFACT_DIRS)]

block_cipher = None

a = Analysis(
    [os.path.join(os.path.dirname(os.path.abspath(SPEC)), "entry.py")],
    pathex=[os.getcwd(), os.path.join(os.getcwd(), "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wells",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wells",
)
