$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
py -m pip install --upgrade pip
py -m pip install ".[build,test]"
py -m pytest
py -m PyInstaller --noconfirm --clean "v2r-auto-posting.spec"

Write-Host ""
Write-Host "빌드 완료: dist\V2R-Auto-Posting.exe"
