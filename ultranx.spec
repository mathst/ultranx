# -*- mode: python ; coding: utf-8 -*-
"""Spec do PyInstaller: binário único e portátil, sem console.

Uso:

    pyinstaller ultranx.spec --noconfirm

O resultado fica em ``dist/UltraNX`` (Linux) ou ``dist/UltraNX.exe`` (Windows).
``console=False`` corresponde a ``--noconsole``; nesse modo o único registro é o
arquivo de log em ``~/.ultranx/logs/``.
"""

from pathlib import Path

block_cipher = None

# Módulos Qt não usados: cortá-los reduz o binário em centenas de MB.
EXCLUDED = [
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtQuick",
    "PyQt6.QtQml",
    "PyQt6.Qt3DCore",
    "PyQt6.QtMultimedia",
    "PyQt6.QtBluetooth",
    "PyQt6.QtDesigner",
    "PyQt6.QtTest",
    "tkinter",
    "unittest",
    "pytest",
]

analysis = Analysis(
    # Precisa ser o launcher, não src/ultranx/__main__.py: o PyInstaller roda o
    # alvo como módulo de topo e os imports relativos do pacote falhariam.
    ["launcher.py"],
    pathex=[str(Path("src"))],
    binaries=[],
    datas=[],
    hiddenimports=[
        "ultranx",
        "ultranx.ui.main_window",
        # py7zr carrega os codecs por nome; a analise estatica nao os ve.
        "py7zr",
        "_lzma",
        "bz2",
        "zlib",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="UltraNX",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # equivalente a --noconsole
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
