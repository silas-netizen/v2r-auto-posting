# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")

a = Analysis(
    ["main_exposure.py"],
    pathex=[],
    binaries=selenium_binaries,
    datas=selenium_datas,
    hiddenimports=list(selenium_hiddenimports)
    + ["v2r_auto.exposure_sheet", "v2r_auto.sheet_values"],
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
    name="V2R-Exposure-Checker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version="exposure_file_version.txt",
)
