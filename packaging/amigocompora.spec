# -*- mode: python ; coding: utf-8 -*-
"""Spec de PyInstaller para empaquetar Amigocompora como ejecutable de Windows.

Uso:
    uv run pyinstaller packaging/amigocompora.spec

Produce `dist/Amigocompora/Amigocompora.exe`. Se empaqueta en modo carpeta
(`COLLECT`) y no en un único fichero (`--onefile`) a propósito: PySide6 carga
plugins de Qt desde disco y el arranque de un onefile descomprime decenas de
megas cada vez, lo que en una app que abre en <1 s es inaceptable.

Los motores se descubren por entry points (`importlib.metadata`), así que hay
que declararlos como hidden imports; si no, PyInstaller no sigue las cadenas de
texto del `pyproject.toml` y los motores no se cargarían en el .exe.
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = [
    "amigocompora.engines.geckoterminal",
    "amigocompora.engines.dexscreener",
    "amigocompora.engines.polymarket",
    "amigocompora.engines.llm.providers",
    "keyring.backends.Windows",
    *collect_submodules("qasync"),
]

a = Analysis(
    ["../src/amigocompora/__main__.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Amigocompora",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # app de ventana, sin consola
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Amigocompora",
)
