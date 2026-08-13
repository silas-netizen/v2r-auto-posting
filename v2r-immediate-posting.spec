# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")
openpyxl_datas, openpyxl_binaries, openpyxl_hiddenimports = collect_all("openpyxl")
pillow_datas, pillow_binaries, pillow_hiddenimports = collect_all("PIL")
pywinauto_datas, pywinauto_binaries, pywinauto_hiddenimports = collect_all("pywinauto")

a = Analysis(
    ["main_immediate.py"],
    pathex=[],
    binaries=(
        selenium_binaries
        + openpyxl_binaries
        + pillow_binaries
        + pywinauto_binaries
    ),
    datas=selenium_datas + openpyxl_datas + pillow_datas + pywinauto_datas,
    hiddenimports=(
        selenium_hiddenimports
        + openpyxl_hiddenimports
        + pillow_hiddenimports
        + pywinauto_hiddenimports
    ),
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
    name="V2R-Immediate-Posting",
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
