# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")

a = Analysis(
    ["main_search_volume.py"],
    pathex=[],
    binaries=selenium_binaries,
    datas=selenium_datas,
    hiddenimports=selenium_hiddenimports
    + [
        "v2r_auto.search_volume",
        "v2r_auto.gui_search_volume",
        "v2r_auto.exposure",
        "v2r_auto.exposure_naver",
        "v2r_auto.exposure_notion",
        "v2r_auto.exposure_sheet",
    ],
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
    name="V2R-Search-Volume",
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
