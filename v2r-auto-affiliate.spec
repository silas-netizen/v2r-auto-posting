# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")
pillow_datas, pillow_binaries, pillow_hiddenimports = collect_all("PIL")

a = Analysis(
    ["main_affiliate.py"],
    pathex=[],
    binaries=selenium_binaries + pillow_binaries,
    datas=selenium_datas + pillow_datas,
    hiddenimports=selenium_hiddenimports + pillow_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="V2R-API-Optimized-Affiliate",
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
