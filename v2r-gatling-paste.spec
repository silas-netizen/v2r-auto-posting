# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")
openpyxl_datas, openpyxl_binaries, openpyxl_hiddenimports = collect_all("openpyxl")
pyxlsb_datas, pyxlsb_binaries, pyxlsb_hiddenimports = collect_all("pyxlsb")

a = Analysis(
    ["main_gatling_paste.py"],
    pathex=[],
    binaries=selenium_binaries + openpyxl_binaries + pyxlsb_binaries,
    datas=selenium_datas + openpyxl_datas + pyxlsb_datas,
    hiddenimports=selenium_hiddenimports + openpyxl_hiddenimports + pyxlsb_hiddenimports,
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
    name="V2R-Gatling-Paste",
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
