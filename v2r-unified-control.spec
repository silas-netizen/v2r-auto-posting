# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
openpyxl_datas, openpyxl_binaries, openpyxl_hiddenimports = collect_all("openpyxl")
pillow_datas, pillow_binaries, pillow_hiddenimports = collect_all("PIL")
crypto_datas, crypto_binaries, crypto_hiddenimports = collect_all("cryptography")

a = Analysis(
    ["main_control.py"],
    pathex=[],
    binaries=(
        playwright_binaries
        + openpyxl_binaries
        + pillow_binaries
        + crypto_binaries
    ),
    datas=(
        playwright_datas
        + openpyxl_datas
        + pillow_datas
        + crypto_datas
    ),
    hiddenimports=(
        playwright_hiddenimports
        + openpyxl_hiddenimports
        + pillow_hiddenimports
        + crypto_hiddenimports
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "selenium",
        "v2r_auto.affiliate_api",
        "v2r_auto.immediate_api",
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
    name="V2R-Playwright-Web",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
