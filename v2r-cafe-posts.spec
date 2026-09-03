# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")

a = Analysis(
    ["main_cafe_posts.py"],
    pathex=[],
    binaries=selenium_binaries,
    datas=selenium_datas,
    hiddenimports=list(selenium_hiddenimports)
    + collect_submodules("v2r_auto")
    + [
        "v2r_auto.gui_cafe_posts",
        "v2r_auto.cafe_post_browser",
        "v2r_auto.cafe_posts",
        "openpyxl",
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
    name="V2R-Cafe-Posts",
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
)
