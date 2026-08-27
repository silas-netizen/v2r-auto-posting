$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
py -m pip install --upgrade pip
py -m pip install ".[build,test]"
py -m pytest
py -m PyInstaller --noconfirm --clean "v2r-gatling-paste.spec"

Write-Host ""
Write-Host "빌드 완료: dist\V2R-Gatling-Paste.exe"
